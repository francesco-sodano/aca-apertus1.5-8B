# Apertus 1.5 on Azure

Apertus is Switzerland's open multilingual model family. Its training corpus
spans about 15 trillion tokens and more than 1,000 languages, with substantial
non-English representation including Swiss German and Romansh.

This deployment runs the 8B model on an Azure Container Apps serverless A100.
Web evidence is used when the Tools profile selects Web Search; it is not
required for every answer. Text is screened by Azure AI Content Safety before
display. Images are checked for harm categories, but embedded image text is
not OCRed and sent through Prompt Shield.

Raw audio is the explicit exception: it is sent directly to Apertus and is not
moderated by Content Safety.

Operational traces contain metadata by default. Operator-enabled content capture
records unredacted text prompts, answers, and tool data under the existing
telemetry access and retention settings. Application authentication fields,
system/developer messages, attachment payloads, and dedicated reasoning fields
are excluded; sensitive information inside ordinary text is not redacted.