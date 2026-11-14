<!--docs
title = "Deployment and operations"
summary = "Installing openWeightGym, where each part runs, its settings, pairing nodes and updating installations."
order = 70
audience = "admin"
covers = ["/docs/operations"]
-->
# Deployment and operations

## Installing and updating {#operations}

Start with [Installation](/docs/operations/installation): one installer sets up the console, a one-time pairing code adds each GPU node to it, and the `owg` command looks after an installation afterwards. [Where things run](/docs/operations/topology) says which programs belong on which machine, and [Settings](/docs/operations/settings) where their settings come from.

To install a node from the app, generate its one-time installer command under [Settings → Compute nodes](/compute-nodes#add-node). Use `owg update` on an installed machine to update it; see [Node installation and updates](/docs/operations/deploying). [System → Components](/system/components) shows what each machine has installed. Editing a checkout alone may not change a running installation.

## When something goes wrong {#trouble}

For a failed reply, start at [Logs → Model requests](/requests), then follow its diagnostics, trace and system-log links. [Status](/status) shows the queue and which models are loaded, [Telemetry](/telemetry) shows each machine's resources and [Alerts](/alerts) lists incidents. A reading that is unavailable is not proof that something has recovered. Restarting a service can interrupt requests and calls under way.
