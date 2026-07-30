# Apertus frontend

Chainlit 2.11.1 frontend for the grounded Apertus deployment.

## Local setup

```powershell
uv sync --dev
uv run pytest
```

To run the UI, authenticate with Azure and set the required environment values:

```powershell
$env:MODEL_ENDPOINT = 'https://<vllm-host>/v1'
$env:MODEL_ID = 'swiss-ai/Apertus-v1.5-8B'
$env:VLLM_API_KEY = '<vllm-api-key>'
$env:CONTENT_SAFETY_ENDPOINT = 'https://<account>.cognitiveservices.azure.com'
$env:FOUNDRY_PROJECT_ENDPOINT = 'https://<account>.services.ai.azure.com/api/projects/<project>'
$env:FOUNDRY_GROUNDING_MODEL = 'gpt-5-mini'
$env:MODEL_HEALTH_TOKEN = '<operator-health-token>'
uv run chainlit run app.py --host 127.0.0.1 --port 8000
```

`DefaultAzureCredential` supplies Content Safety and Foundry tokens. For local
development, `az login` is usually sufficient. The service refuses text/image
requests when a mandatory policy or grounding dependency is unavailable.

Raw audio is intentionally not moderated. See
[security-and-grounding.md](../../docs/security-and-grounding.md).