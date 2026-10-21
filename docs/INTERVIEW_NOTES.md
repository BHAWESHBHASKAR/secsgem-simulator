# Interview notes

Personal prep notes for talking about this project. Everything here is tied to code in
this repo. Where the project simplifies the SEMI standards, the notes say so; it is
better to raise that yourself than to have an interviewer find it.

---

## 1. Two-minute pitch

> I wanted to understand how a fab host actually talks to process tools, so I built a
> SECS/GEM equipment simulator from scratch in Python, using only the standard
> library and asyncio and no SECS library.
>
> It has three layers, one per SEMI standard. The bottom layer is a SECS-II codec
> (E5). It encodes and decodes all 14 data item formats I support, including nested
> lists and the 1-to-3 length-byte rule, and it prints messages in SML so you can read
> them. The middle layer is HSMS (E37) over TCP: the 10-byte header, the
> Select/Linktest/Separate control messages, the connection state machine, and the
> T3, T6, T7 and T8 timers. Requests and replies are paired by system bytes, so
> several transactions can be open at once.
>
> On top of that is a GEM (E30) subset for a simulated etch chamber. It has the
> communication and control state models, a process state machine, remote commands
> with proper HCACK rejections, equipment constants, host-defined event reports,
> alarms, and S9 error messages. While a recipe runs, a background task drifts the
> chamber temperature and pressure toward their setpoints. If the temperature passes
> a limit, the tool sends an S5F1 alarm. In the demo, the host reacts by sending
> ABORT, and the tool answers with a ProcessAborted event report.
>
> There is also a host client with an async API, a CLI that runs a scripted end-to-end
> scenario, and 112 pytest tests. They include real localhost integration tests and
> timer tests like T3 and T7. I kept a list of what I left out of E30, such as
> spooling and HSMS-GS, so I'm clear about what's a simulator and what a production
> GEM interface would need.

Shorter version (30 s): "A from-scratch SECS-II/HSMS/GEM stack in Python asyncio:
binary codec, TCP transport with timers and transaction matching, and a simulated etch
tool with state machines, event reports, alarms and remote commands, plus a host client
and 112 tests."

---

## 2. Walkthrough: one remote START, end to end

Setup already done: TCP connected, HSMS SELECTED, S1F13 accepted (COMMUNICATING),
S1F17 accepted, operator switch on REMOTE, so the tool is ONLINE/REMOTE. Reports are
defined and linked, and events are enabled.

**Step 1: host builds the message** (`host.py: remote_command`)

```
S2F41 W
<L [2]
  <A "START">                       RCMD
  <L [1] <L [2] <A "PPID"> <A "ETCH_OXIDE_01">>>   CPNAME / CPVAL pairs
>
```

**Step 2: HSMS layer sends it** (`hsms.py: send_request`)
- Allocates the next system bytes (here 0x0000000F).
- Because the W-bit is set, stores a Future in `_pending_replies[0x0F]`.
- Writes the frame, then waits on the Future for up to T3 (45 s by default).

Real bytes on the wire (computed with this code):

```
00 00 00 2c                  length = 44 (10 header + 34 body)
00 00                        session id = device id 0
82                           W-bit (0x80) | stream 2
29                           function 41
00                           PType 0 = SECS-II
00                           SType 0 = data message
00 00 00 0f                  system bytes
01 02                        <L [2]
41 05 53 54 41 52 54           <A "START">        (0x41 = A format 0o20 << 2 | 1 length byte)
01 01                          <L [1]
01 02                            <L [2]
41 04 50 50 49 44                  <A "PPID">
41 0d 45 54 43 48 ...              <A "ETCH_OXIDE_01">  (13 bytes)
```

**Step 3: equipment receives it** (`hsms.py: _read_loop -> _handle_data`)
- Reads the 4-byte length, then the rest of the message, with T8 as the deadline.
- The state is SELECTED, so the data message is accepted (otherwise it would send
  Reject.req with reason 4).
- Decodes the body. The function number is odd, so this is a new primary. It is
  handed to the GEM handler in a new task, so the read loop keeps reading.

**Step 4: GEM checks** (`equipment.py: handle_message -> _s2f41_remote_command`)
1. Device id matches? (no → S9F1)
2. Known stream/function? (no → S9F3 / S9F5)
3. COMMUNICATING and not OFFLINE? (no → S2F0)
4. Body shape valid? (no → S9F7)
5. Known RCMD? (no → HCACK 1)
6. ONLINE/REMOTE? (no → HCACK 2)
7. Parameter names allowed? (no → HCACK 3 + CPACK 1)
8. Process state allows START? (EXECUTING/PAUSED → HCACK 5, SETUP → HCACK 2)
9. PPID known? (no → HCACK 3 + CPACK 2)

**Step 5: state change, then reply.** The process state moves IDLE → SETUP
*synchronously*, a background task is started for the recipe, and the handler returns:

```
S2F42                                     bytes: 00 00 00 11 | 00 00 02 2a 00 00 00 00 00 0f
<L [2] <B 0x04> <L [0]>>                         01 02 21 01 04 01 00
```

HCACK 4 means "accepted, completion will be signalled by events". The reply has the
**same system bytes 0x0F** and no W-bit.

**Step 6: host matches the reply.** Its read loop sees an even function with system bytes
0x0F, finds the Future in `_pending_replies` and completes it. `remote_command()`
returns `RemoteCommandResult(hcack=ACK_FINISH_LATER)`.

**Step 7: the recipe runs** (`equipment.py: _setup_then_execute -> _execute`)
- After `setup_time`, the state moves SETUP → READY → EXECUTING and `emit_event(3001)`
  runs.
- `emit_event` checks that the event is enabled and reporting is possible, reads the
  report's VIDs *right now*, builds the S6F11 and puts it on the outbox queue.
- The sender task sends it as a W-bit primary using the *equipment's own*
  system-bytes counter, then waits for S6F12. If no reply comes within T3, it sends
  S9F9.
- The host's handler parses the event, completes any `expect_event(3001)` futures,
  calls the callbacks and replies `S6F12 <B 0x00>`.
- Each wafer sends WaferCompleted (3003). At the end the state goes EXECUTING → IDLE
  and ProcessCompleted (3002) is sent.

---

## 3. Likely questions and answers

**Q1. What is the difference between SECS-I, SECS-II, HSMS and GEM?**
SECS-II (E5) is the message format: streams, functions and typed data items. SECS-I
(E4) and HSMS (E37) are two transports for those messages. SECS-I is the old RS-232
serial link; HSMS is TCP/IP. GEM (E30) sits on top and defines *behaviour*: which
messages a tool must support and the state models it must follow, so one host can
drive many tool types the same way. This project implements E5, E37 (HSMS-SS) and a
subset of E30. It does not implement SECS-I.

**Q2. Walk me through bringing a tool online.**
TCP connect (the host is usually active, the tool passive). Then HSMS Select.req and
Select.rsp with status 0, which moves the connection to SELECTED. Before Select, data
messages get Reject.req. Then S1F13/S1F14 makes GEM communication COMMUNICATING. Then
S1F17/S1F18 asks for ONLINE. The tool goes to ONLINE/REMOTE or ONLINE/LOCAL depending
on the operator's switch. The demo shows that S1F1 sent while still OFFLINE gets S1F0.

**Q3. What are system bytes?**
A 4-byte transaction id in the HSMS header. The sender picks a new value for each
primary message, and the reply copies it. That is how a reply finds its request,
especially when several are in flight. In `hsms.py` there is a dict from system bytes
to an `asyncio.Future`. Each side numbers its own primaries, which is why the
equipment's first S6F11 also has system bytes 1.

**Q4. What is the W-bit, and what happens if the reply never comes?**
The W-bit (top bit of header byte 2) means "reply expected". The sender waits up to
**T3**. On timeout, `send_request` raises `ReplyTimeoutError`. The link is not closed;
only that transaction failed. Per E5, when the equipment's T3 expires it sends
**S9F9** (Transaction Timer Timeout) with the header of the unanswered message. The
sender loop does exactly that, and a test covers it.

**Q5. Explain the HSMS timers.**
- T3: reply timeout for data messages.
- T5: connect separation, the minimum delay between active connect attempts.
- T6: control transaction timeout (waiting for Select/Deselect/Linktest .rsp); on
  expiry the connection is considered broken and is closed.
- T7: not-selected timeout; the passive side drops a TCP connection that never
  selects.
- T8: network intercharacter timeout, the maximum gap between bytes of one message.

Defaults here: 45/10/5/10/5 s. Simplification: T8 is applied to the whole remainder
of a message, not to each gap between bytes.

**Q6. Why linktest if TCP already detects failures?**
TCP can sit in a half-open state for a very long time, for example if a cable is
pulled or a switch reboots and nothing is being sent. Keepalives are slow and
OS-dependent. Linktest is an application-level heartbeat with a T6 deadline: if the
peer doesn't answer, `_control_transaction` closes the connection, and the higher
layers drop to NOT COMMUNICATING.

**Q7. Active vs passive mode?**
Passive opens a listening socket and accepts. Active connects. Usually the
equipment is passive and the host is active, as here. The passive side owns T7. The
active side retries the connect with T5 between attempts. HSMS-SS allows one
connection, so `HsmsPassiveServer` refuses a second one.

**Q8. Explain the GEM control state. Why can't the host just switch to REMOTE?**
OFFLINE means the tool ignores the host except for S1F13 and S1F17; other primaries
get SxF0. In ONLINE/LOCAL the host can monitor, but remote commands get HCACK 2. In
ONLINE/REMOTE the host can command the tool. LOCAL vs REMOTE is an operator decision
at the tool: someone may be working on it, so the host must not take control. In the
code, S1F17 picks the substate from `operator_remote`, and only
`set_operator_switch()` (the "front panel") can change it. Simplification: E30's
three OFFLINE substates are collapsed into one.

**Q9. How do event reports work?**
Dynamic event report configuration has three steps. S2F33 defines a report: RPTID →
list of VIDs. S2F35 links reports to a collection event: CEID → RPTIDs. S2F37 enables
the event. When the event happens, the tool sends S6F11 with DATAID, CEID and, for
each linked report, the current values of its VIDs. The host decides what data it
wants without a tool software change. The acks (DRACK/LRACK/ERACK) report unknown
IDs or duplicates, and the code validates the whole request before applying any of it.

**Q10. SVID vs DVID vs ECID?**
SVIDs are status values that are valid at any time and can be polled with S1F3
(ChamberTemp, ProcessState). DVIDs are data values that are only meaningful when an
event happens (PPID, CurrentWafer); you get them in event reports, not by polling.
ECIDs are settings the host can read or write with S2F13/S2F15 (TempSetpoint),
range-checked here, with EAC 3 if out of range.

**Q11. HCACK 0 vs 4?**
0 means "done". 4 means "accepted, and completion will be reported later by an
event". START returns 4 because the run takes minutes and the host learns the
outcome from ProcessStarted, WaferCompleted and ProcessCompleted. PAUSE, RESUME and
ABORT finish immediately here, so they return 0. Rejections: 1 unknown command, 2
can't do it now (not REMOTE, or wrong state), 3 bad parameter (with per-parameter
CPACKs), 5 already in that state (PAUSE while paused).

**Q12. How do alarms work?**
S5F1 carries ALCD, ALID and ALTX. ALCD bit 8 means set (1) or cleared (0); the low
bits are the category (here 2 = equipment safety). The host answers S5F2. S5F3
enables or disables *reporting* of an alarm, but the tool still tracks the alarm's
state; a test checks that a disabled alarm is active without being sent. S5F5 lists
the alarms with their current set/cleared state. The over-temp alarm uses hysteresis:
it clears only when the temperature is 2 °C below the limit, so it doesn't flap.

**Q13. When are S9 messages sent?**
They are sent by the equipment when it cannot process a message at all. S9F1 for an
unknown device id, S9F3 for an unknown stream, S9F5 for an unknown function, S9F7 for
illegal data (wrong structure or undecodable bytes), and S9F9 for T3 expiry. The body
is the 10-byte header of the offending message. The original transaction gets no
normal reply, so the host client reads the system bytes out of the S9 body and fails
that transaction immediately instead of waiting for T3.

**Q14. What is SxF0?**
Function 0 is "abort transaction". It is a reply that says "I received your primary
but will not process it". Here the tool sends it while NOT COMMUNICATING or OFFLINE,
and the host raises `TransactionAbortedError`.

**Q15. How is a SECS-II item encoded?**
Format byte = (6-bit format code << 2) | number of length bytes (1–3). Then the
length, big-endian, then the data. For lists, the length is the number of child
items; for everything else it is the number of bytes. Example: `<A "MDLN">` = `41 04
4D 44 4C 4E`. A 256-byte binary item needs 2 length bytes (`22 01 00 ...`). The
largest item is 2^24−1 bytes. The tests check the 255/256 and 65535/65536 boundaries.

**Q16. Why asyncio, and what concurrency bugs did you have to think about?**
One thread, many concurrent activities (read loop, timers, simulation, recipe,
outbound reports), and no locks around shared state, because code only yields at an
`await`. Things I handled:
- **Select race.** State changes when Select.rsp is processed in the read loop, so
  a data message right behind it isn't rejected.
- **Double START.** The state transition happens before the reply is sent, so a
  second START is rejected.
- **Handlers that block.** Primaries run in their own task, so a handler that waits
  on something can't stall the socket.
- **Concurrent drain().** An `asyncio.Lock` serialises writes.
- **Event-wait race.** `expect_event()` registers interest *before* the command is
  sent.

**Q17. How did you test it?**
112 pytest tests. Codec tests compare exact bytes and run round trips at the length
boundaries. HSMS tests use real localhost sockets: concurrent requests matched by
system bytes, Reject.req before Select, T3 with a handler that never replies, T6
with a fake peer that accepts TCP but never answers, and T7 with a raw socket that
never selects. Integration tests start the equipment and host on an ephemeral port.
One test runs the entire demo in-process. Timers are shortened (T3 = 2 s, wafer
time 0.2 s) so the suite runs in about 10 s. I ran it repeatedly to check for
flakiness.

**Q18. What would a production GEM interface need that this doesn't have?**
- Spooling, so messages aren't lost while the link is down.
- A persisted report/link/enable configuration.
- Alarm-linked collection events.
- Process program management (S7) and terminal services (S10).
- Equipment-initiated S1F13 with CommDelay retry.
- Automatic reconnect.
- A tool model loaded from configuration instead of code.
- For 300 mm fabs: the GEM300 standards, i.e. E40 process jobs, E94 control jobs,
  E87 carrier management, E90 substrate tracking and E116 equipment performance
  tracking.
- Interface A (EDA) for high-rate data collection.
- On the host side: multiple tools, metrics, and alerting on T3/T6 failures.

**Q19. A tool "stopped sending events". How would you debug it?**
Work up the layers:
1. Is TCP connected, and is HSMS SELECTED? Look for linktest failures, T6, or T7
   drops in the log.
2. Is GEM COMMUNICATING? Did the link drop and nobody re-sent S1F13?
3. Is the control state ONLINE?
4. Are the events still enabled and linked? Some tools lose the configuration on
   restart; the host should re-send S2F33/35/37 after every S1F13.
5. Is the host acknowledging S6F11 in time? Look for S9F9.
6. Check the message trace by system bytes.

The JSON log format with `sxfy`, `direction` and `system_bytes` fields makes this
searchable.

---

## 4. Numbers to remember

- 14 primary messages handled by the equipment: S1F1/3/11/13/15/17, S2F13/15/33/35/37/41,
  S5F3/5.
- Equipment-initiated messages: S5F1, S6F11 and S9F1/3/5/7/9.
- HSMS control messages: Select, Deselect, Linktest (req/rsp), Separate, Reject.
- 14 SECS-II formats: L, B, BOOLEAN, A, I1/I2/I4/I8, U1/U2/U4/U8, F4/F8.
- 112 tests; the suite runs in about 10 s.
- A full demo run: 39 HSMS messages sent and 38 received by the host, 11 event
  reports, 2 alarm reports, in about 4 s.
