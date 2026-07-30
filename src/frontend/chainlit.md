# Apertus 1.5 on Azure

Apertus is Switzerland's open multilingual model family. Its training corpus
spans about 15 trillion tokens and more than 1,000 languages, with substantial
non-English representation including Swiss German and Romansh.

This deployment runs the 8B model on an Azure Container Apps serverless A100.
Every answer requires current, cited Web Search evidence before Apertus is
invoked. Text and images pass Azure AI Content Safety input and output gates.

Raw audio is the explicit exception: it is sent directly to Apertus and is not
moderated by Content Safety.