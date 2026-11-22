---
name: phonectl-scripts
description: Rules for a script that drives the iPhone through the shared phonectl server (the `phone` MCP server, http://127.0.0.1:47321/mcp), and for running one. Use before writing or changing such a script, and before starting one that runs for more than a few minutes.
---

# Phone scripts on the shared phonectl server

Several agents share one iPhone through one server, and take turns. A script is one more
client of that server. These rules keep a script from tapping a screen another agent changed,
from failing while it waits for its turn, and from keeping the others off the phone for its
whole run.

## Writing the script

**Connect to the server, never to the phone.** Use an MCP client library (the official Python
SDK: `mcp>=2.3,<3`) over streamable HTTP, with the header
`Authorization: Bearer <contents of ~/.config/phonectl/token>`. Connecting to the phone directly
cuts the server off from it. Leave `X-Phonectl-Client` unset: a script an agent starts then
counts as that agent and does not wait for it.

**Send every tool call through one helper that knows three answers:**

- `WAITING, nothing was done: ...` (an error result): another client has the phone. Call the
  same tool again at once with the same arguments. Each call waits in line up to 50 s, and the
  place in line is kept between calls. This is not a failure: never stop or skip on it.
- `NOT DONE: ...` (an error result): someone else used the phone since the script last looked,
  so the input was not sent. Look again (`read_text` or `screenshot`), find the target again,
  then repeat the input if it still applies.
- Any other error result: stop the script and report the text. Do not carry on as if the
  input had landed.

```python
class PhoneError(RuntimeError):
    pass

async def call(phone, tool, **args):
    while True:
        result = await phone.call_tool(tool, args)
        text = " ".join(c.text for c in result.content if c.type == "text")
        if not result.is_error:
            return result
        if not text.startswith("WAITING"):
            raise PhoneError(text)  # NOT DONE included: the caller looks again, then retries
```

**Look before a blind input.** `tap` at coordinates, `swipe`, `long_press` and `type_text` act
on what an earlier look showed. Prefer `tap_text`, which finds its target itself.

**Work in units, and give the phone up between them.** A unit is one piece of work that stands
on its own: one application sent, one reply posted. After each unit:

1. Record that it is done (a file the script reads at start), so a rerun skips it.
2. `POST http://127.0.0.1:47321/phonectl/yield` with the same token header. If another client
   is waiting, the phone goes to it now, and the script's next call waits in line; if nobody
   waits, nothing changes. A script that never yields keeps everyone else waiting until it
   pauses, or for up to 30 minutes.
3. Start the next unit from a known place: open the app (`launch_app`), look at the screen,
   and navigate from what is there. After a yield, the screen may show anything.

```python
import json, urllib.request
from pathlib import Path

def yield_phone():
    token = (Path.home() / ".config/phonectl/token").read_text().strip()
    req = urllib.request.Request("http://127.0.0.1:47321/phonectl/yield", data=b"{}", method="POST",
                                 headers={"Authorization": f"Bearer {token}"})
    return json.load(urllib.request.urlopen(req, timeout=10))["yielded"]
```

Never yield in the middle of a unit (with a half-typed message or an open picker): the next
client would act on that screen state, and the unit would have to start over.

## Running a long script

- It may pause for minutes while another agent has the phone. That is the queue working, not
  a hang. Run it so that your own tool timeout cannot kill it: in the background with its
  output in a log file, or with a timeout longer than the whole run.
- Judge it by its exit code and its log. Do not kill or restart it because it went quiet; a
  restart repeats work unless the script records what it has done.
- If it stops on a screen it does not know, read its log (and any screenshot it saved) before
  you run it again.
