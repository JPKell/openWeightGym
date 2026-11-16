<!--docs
title = "Classifier"
summary = "Ask typed questions and compare uncalibrated decision probabilities."
order = 10
covers = ["/chat/classifier"]
-->
# Classifier

Ask typed questions about text, chat or JSON, and compare decision probabilities.

## Before you start

Use a permitted classifier model. These probabilities are uncalibrated; check them on your own examples.

## Do it

1. Open **Classifier**, choose **Model** and a compute node, and enter **State** as **Text**, **Chat** or **JSON**.
2. Use a **Question set** starter or **Add question**. Set its **ID**, **Type** and **Instructions**; add options or ordered score criteria.
3. Select **Classify**. Inspect the distribution, latency and input tokens. **Compare with (up to 2)** runs the same questions on additional models.

## Options

**Developer drawer** shows request/response JSON and client examples using the configured model API scheme, independently of the console’s HTTPS scheme. Replace the base address when your client uses a different API proxy. **History in this tab** supports **Export JSON** and **Export CSV**; reloading loses that tab history.

<!-- audience: admin -->
## Install classifiers

Open **Install classifiers**, choose a compute node, select **Download starter set · 13 GB**, then **Install downloaded classifiers** after the downloads finish. Downloads must be enabled. The link **Classifier files and conversion jobs** opens the filtered file library.
<!-- /audience -->

## Limits and troubleshooting

**No permitted classifier is installed.** means this account has no usable classifier. Ask an administrator to install one or check model access. See [Tools](/docs/web-app/tools) for MCP access.
