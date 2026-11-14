<!--docs
title = "API reference"
summary = "The model API that OpenAI-compatible clients use, openWeightGym's own endpoints, and the headers both accept."
order = 20
audience = "admin"
covers = ["/docs/api"]
-->
# API reference

## Connection and scope {#api-connection}

Use Turnstile's model listener, normally `http://CONTROL_NODE:8442/v1`, as an OpenAI client's base URL. Send `Authorization: Bearer API_KEY` using your openWeightGym account key. A configured model or role name replaces an OpenAI model name. Model permissions, engine capabilities, installed runtimes and node availability still apply.

This reference describes the current source, not a guarantee that every model or backend supports every request field. The [custom endpoint inventory](/docs/api/custom) reads route declarations without running services. Browser console forms and static assets are outside it. Worker model APIs should be reached through Turnstile: workers validate its signed caller statements.

Compatibility terminology checked against the [official OpenAI API reference](https://developers.openai.com/api/reference/overview) on 2026-10-26. openWeightGym support is established by the [code reference](/docs/code/api).
