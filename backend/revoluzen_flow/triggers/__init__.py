"""Event triggers: what starts a pipeline other than the clock or a person.

* ``file_watch``: a new file in a folder (on this server, an SFTP server, a
  Windows file share, S3, Google Cloud Storage or Azure Blob Storage) starts one
  run, with the file's path as a run parameter. What was seen is remembered, so
  a file never starts two runs.
* ``chain``: another pipeline finishing (on success, on failure, or either way)
  starts a run. Loops are refused when the trigger is saved and again at run
  time.
* ``webhook_signing``: calls to a published endpoint can be signed with a
  shared secret instead of carrying an API key. The signature is checked, and a
  message is accepted once, inside a short time window.

The triggers are rows in the ``schedules`` table (``schedule_type`` is
``file_arrival`` or ``after_pipeline``) and run through
``PipelineScheduler._run_pipeline``, the same path as a timed schedule. So a
triggered run is admitted by the pool, honours the pipeline's run time limit and
overlap setting, records its run, and reads the published version only.
"""
