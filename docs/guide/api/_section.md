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

Building an application, or briefing an AI agent to build one? Give it [Build an app on the API](/docs/api/app-builder): one self-contained page with every client endpoint, field and error. Otherwise start with [Authentication and keys](/docs/api/authentication), then try the [examples](/docs/api/examples). Route and setting inventories are generated from this checkout.
