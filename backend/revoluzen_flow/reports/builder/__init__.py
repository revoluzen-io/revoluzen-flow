"""Reports people design themselves.

The Insights > Reports page lets someone pick what goes into a report, narrow
it to the environment, projects, pipelines and period they care about, save
it, and have it sent on a schedule.

    models.py   what a saved report is, and the checks on it
    schedule.py when it goes out, and what period "last week" means
    catalog.py  the sections on offer and the starting templates
    collect.py  turns a saved report into real numbers
    render.py   those numbers as email, chat text, PDF and Word
    store.py    saved reports and the log of what went out
    deliver.py  sends one, and records whether it worked

Nothing here runs on its own timer or opens its own mail connection. The
pipeline scheduler's tick decides when a report is due
(``PipelineScheduler._check_reports``), and the mail and webhook sending belong
to ``alerts.notifier.NotificationService``.
"""
