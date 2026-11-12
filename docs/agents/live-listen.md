# Live-listen agent pattern

RGS exposes an authenticated MCP surface at `/mcp`. Connect with a verified
human identity accepted by forwarded-auth, then list your accessible sessions,
wait for events, claim a `gate.trigger`, read the corresponding transcript
and post a finding. Do not assume a mock gate trigger is a factual answer:
the offline gate only recognizes a trailing question mark.

Treat call audio and transcripts as sensitive user content. Only use sessions
whose participants consent to capture; avoid forwarding transcript data to
external models without the participants' and operator's approval. The core
does not grant machine accounts blanket session access. A separate explicit
authorization adapter is needed to let service agents act on a person's behalf.
