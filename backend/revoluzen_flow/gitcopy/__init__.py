"""A copy of every published pipeline in the customer's own Git repository.

Versions live in Flow's own database, so they cannot help if that database is
lost. When someone publishes, unpublishes, deploys or rolls back a pipeline,
this package writes it to a Git repository the customer owns, as files people
can read, with who did it and why as the commit.

* ``settings``  where the repository is and the access token (kept encrypted).
* ``files``     what is written, and the check that no secret goes with it.
* ``client``    runs the ``git`` program, with the token out of its arguments,
  its config and its output.
* ``store``     one row for each copy to make, so a failed one can be retried.
* ``service``   makes the copies in the background.

Git is a copy, not the source of truth: Flow keeps working if the repository
cannot be reached, and says so on the pipeline.
"""
