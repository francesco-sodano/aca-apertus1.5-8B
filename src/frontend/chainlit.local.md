# Apertus 1.5 on your laptop

The original Swiss Apertus 1.5 8B runs on your NVIDIA GPU through the Swiss
vLLM runtime. This frontend and inference are local: no Azure services,
authentication, cloud moderation, web search, or cloud telemetry.

Use **Chat** for text, images and audio; **Tools** adds an allowlisted local
calculator and clock; **Thinking** enables reasoning without displaying hidden
reasoning or invoking tools.

Send at most one image and one audio file per turn (20 MiB each). Images are
resized to fit 512 x 512; audio is decoded locally to mono 24 kHz PCM and must
be at most ten seconds long. The shared input/output context cap is 16K.

**Known limitation:** fresh media probes passed, but contextual audio answers
were incorrect in additional numeric-text tests. Verify important answers.
Only text from prior turns is retained; previous media is not re-sent.
This model understands media and generates text, not images or speech.

The UI has no login and must remain bound to localhost. Prompts and uploads
are not sent to Azure or another inference provider.
