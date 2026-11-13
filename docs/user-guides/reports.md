# Reports

Open **Insights → Reports**. You design a report once, then read it whenever you like or have it sent on a schedule.

## Make a report

1. Click **+ New report**, or start from a template under **Start from**.
2. Give it a name.
3. **What goes in.** Add the sections you want and put them in order. Every number comes from what Flow has recorded:

| Section | What it shows | Where the numbers come from |
|---|---|---|
| Run summary | Runs, successes, failures, stopped runs, success rate and average run time, compared with the period before | Run history |
| Runs over time | Successes and failures per hour, day or week | Run history |
| Failures | Which pipelines failed, how often, the failing step and the most common error | Run history |
| Successful pipelines | Successes, success rate, run time and rows | Run history |
| Slowest pipelines | Average and longest run time, and how the average moved | Run history |
| How runs were started | Schedule, manual, API and other triggers | Run history |
| Steps that fail most | The individual steps behind the failures | Step results |
| Usage | Pipelines run, rows processed and delivered, run time, CPU time, highest memory | Run history and step results |
| Busiest pipelines | Which pipelines take the most run time, rows and memory | Run history and step results |
| Storage | Files, managed tables, pipeline outputs and their size | Storage |
| AI use | AI requests and tokens by model. Totals only, never what was asked | AI activity |
| Environment | Flow version, scheduler, workers, memory, disk, and connections or credentials that need attention | This server, when the report is made |
| What's in the workspace | Counts of pipelines, projects, connections, schedules and alert rules | Workspace contents |
| New builds | New pipelines, new saved versions, and anything published or deployed. Auto-saves are not counted | Pipeline versions and lifecycle events |
| Schedules | Which schedules are on, when they last ran and how that went | Schedules and run history |
| Alerts sent | Which alert rules fired, and any that could not be delivered | Alert log |
| A table you design | Choose runs, steps, pipeline changes or alerts, what to group by, the columns and the sort | Any of the above |
| A note | Your own words | Typed by you |

4. **Narrow it.** Choose the period, the projects and pipelines, and whether to count every run or only automatic ones (runs someone started by hand while building are left out). Community has one environment, Development, so that choice appears only on Enterprise.
5. Click **Save report**. The preview shows your real numbers while you work.

A period like **Last week** means Monday to Sunday in the report's own time zone. **Last 7 days** ends at the moment the report is made.

A section marked as covering the whole workspace, or the server as it is now, says so under its heading: the filters do not apply to it.

## Send it on a schedule

Under **Schedule and share**:

- **Send it to**: type or paste email addresses.
- **Post the headlines to Slack or Teams**: paste a webhook address, or leave it empty to use the one from **Settings → Notifications**.
- **Send the full report to a webhook**: for another system to read.
- **Attach a PDF to the email**, if you want one.
- **Send this report automatically**: daily, on the days of the week you choose, or on a day of the month from 1 to 28, at a time in the report's time zone.
- **Only when runs failed** skips a send when no run failed in the period. The sent history still notes it.

Email uses the mail server in **Settings → Notifications**, the same one alert rules use. If none is set up the designer says so.

If the server is off at the send time, the report goes out when the server is next running, up to 12 hours late for a daily report, 3 days for weekly and 7 days for monthly. Later than that, it waits for the next send time. Turning a schedule on, or changing its time, does not send for a time that has already passed.

A send that reached nobody, for example because the mail server was down, is tried twice more, ten minutes apart. A send that reached some people is not repeated, so nobody gets it twice.

## See whether it went out

The list of reports shows each one's schedule, its next send and how its last send went. Open a report and choose **Sent history** to see every send: when, what started it, who it went to, and the reason for anything that failed. **What was sent** shows the report exactly as it went out.

If a scheduled send does not fully work, whoever made the report gets a notification in the bell.

**Send now** sends to everyone on the report. **Test to me** sends only to you.

## Keep a copy

**PDF** and **Word** download the report as it stands now, and keep a copy in **Storage → Files**.

## The installation inventory

**Installation inventory**, under **Start from**, is one fixed document listing everything in this install: projects, pipelines, connections, schedules, alert rules and triggers, with passwords and keys left out. It is not tied to a period. Use it for audits and handovers.

## Who can do what

Anyone in the workspace can open and download a report. Making, changing, scheduling and sending one needs at least the Developer role, the same as alert rules.

Error messages in a report have passwords, keys and email addresses removed, because a report can go outside the team. A stored webhook address is encrypted and never shown again.

Webhooks on the private network are refused unless the operator sets `REVOLUZEN_FLOW_REPORT_WEBHOOK_ALLOW_PRIVATE=1`.
