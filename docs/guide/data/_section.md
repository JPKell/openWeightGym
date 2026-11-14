<!--docs
title = "Data and backups"
summary = "Where openWeightGym keeps its data, which program owns each store, and how to back it up, restore it and move it to another database engine."
order = 60
audience = "admin"
covers = ["/docs/data"]
-->
# Data and backups

## Who owns what {#data}

Control node owns accounts and the history people keep: conversations, collections, workflows, benchmark results, logs and media libraries. Each worker owns its model files, its model settings and its own runtime state. Pages and records kept on the control node stay readable while a worker is away.

Every store uses SQLite unless it is moved; PostgreSQL works through the same shared storage layer, and each store can use its own engine. [System → DB](/databases) lists every store with its engine, owner, size and tables, and exports a database or one table as SQL. [System → Backups](/backups) makes recovery sets from both machines and previews a restore before anything changes.

Keep each host's settings, its `keys/` folder and its `data/` folder private.
