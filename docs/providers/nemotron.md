# Nemotron-compatible transcription provider

The offline mock provider needs no model server. To use an external ASR,
set `ASR_PROVIDER=nemotron`, `NEMOTRON_BASE_URL=https://your-provider.example/v1`
and, if needed, `NEMOTRON_API_KEY`. RGS POSTs JSON to
`{NEMOTRON_BASE_URL}/invocations` with a bearer token when a key is set.
Supply your own compatible server and model; **no model weights or provider
account are included**.

The request is an OpenAI-shaped chat-completion object with one user message
containing a transcription prompt and `audio_url.url` as a `data:audio/wav;base64,...`
URI (16-kHz mono PCM16). It includes `model`, `temperature: 0`, `stream: false`, speaker tags (`<spk:0>` etc.) in the prompt and
`mm_processor_kwargs.capture_ctc_timestamps: true`. See
`NemotronClient.build_transcribe_body` for the exact shape. The response must
include `choices[0].message.content` containing words with optional `<spk:N>`
tags, plus optional `ctc_timestamps`:

```json
{
  "choices": [{"message": {"content": "<spk:0> Hello, sample app!"}}],
  "ctc_timestamps": {
    "words": [{"word": "Hello,", "start": 0.1, "end": 0.3, "speaker": 0}],
    "diarization": [{"start": 0.0, "end": 1.0, "speaker": 0}]
  }
}
```

Without CTC words, RGS estimates word timings. If speaker embeddings are
enabled, the same endpoint receives `{"task":"speaker_embed",
"audio_b64":"...","segments":[[0.1,0.8]]}` and returns
`{"embeddings":[[0.1,0.2]]}`; otherwise `LIVE_EMBED_ENABLED=false` avoids
that request. `{"task":"health"}` expects an object response. This is a
*transport contract*, not a promise that an arbitrary vanilla Nemotron server
implements the same JSON protocol: adapt the `NemotronTransport` protocol if
your model server differs.

For a SageMaker endpoint implementing this contract instead of HTTP, install
`uv sync --extra aws` and set `NEMOTRON_ENDPOINT_NAME`,
`NEMOTRON_AWS_REGION` and AWS credentials through the normal AWS credential
chain. Do not commit credentials, audio or transcripts. The public export has
no SageMaker endpoint or AWS account configured.
