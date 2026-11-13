"""Folders: creating a pipeline inside one, and deleting one that holds pipelines.

Two things here were quietly wrong:

* Creating a pipeline with a ``folder_id`` is meant to check that the folder
  exists in the caller's workspace and belongs to the pipeline's project. The
  check called ``FolderStore.get(id, workspace_id=...)``, an argument that method
  does not take; the ``TypeError`` was swallowed by a bare ``except``, so no
  folder was ever checked and a pipeline could point at a folder that does not
  exist, or at one in another project or workspace.
* Deleting a folder that holds pipelines needs an explicit mode
  (``move_to_root`` or ``force``), and the screen sent neither, so it always
  got a 409. These tests pin the three modes the screen now relies on.
"""
from __future__ import annotations

import pytest

from revoluzen_flow.folders.models import Folder
from revoluzen_flow.ir.schema import PipelineStatus

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client, role_clients,
)


def _project(authed_client, name: str) -> str:
    created = authed_client.post("/api/projects/", json={"name": name})
    assert created.status_code == 200, created.text
    return created.json()["id"]


def _folder(authed_client, project_id: str, name: str = "Folder") -> str:
    created = authed_client.post("/api/folders", json={"name": name, "project_id": project_id})
    assert created.status_code == 200, created.text
    return created.json()["id"]


def _create(authed_client, name: str, **extra):
    return authed_client.post("/api/workflows/", json={
        "name": name,
        "steps": [{"id": "src", "type": "source", "params": {"file_path": "orders.csv"}}],
        "connections": [],
        **extra,
    })


def _make(authed_client, name: str, **extra) -> str:
    created = _create(authed_client, name, **extra)
    assert created.status_code == 200, created.text
    return created.json()["id"]


class TestCreatingInsideAFolder:
    def test_a_pipeline_can_be_created_in_a_folder_of_its_project(self, authed_client):
        project_id = _project(authed_client, "Folder check: ok")
        folder_id = _folder(authed_client, project_id)

        created = _create(authed_client, "Folder check: inside", project_id=project_id, folder_id=folder_id)

        assert created.status_code == 200, created.text
        assert authed_client.get(f"/api/workflows/{created.json()['id']}").json()["workflow"]["folder_id"] == folder_id

    def test_a_folder_that_does_not_exist_is_rejected(self, authed_client):
        project_id = _project(authed_client, "Folder check: missing")

        created = _create(authed_client, "Folder check: nowhere", project_id=project_id, folder_id="no-such-folder")

        assert created.status_code == 404, created.text
        assert "no-such-folder" in created.text

    def test_a_folder_of_another_project_is_rejected(self, authed_client):
        project_a = _project(authed_client, "Folder check: A")
        project_b = _project(authed_client, "Folder check: B")
        folder_in_a = _folder(authed_client, project_a)

        created = _create(authed_client, "Folder check: wrong project", project_id=project_b, folder_id=folder_in_a)

        assert created.status_code == 400, created.text
        assert "belongs to project" in created.text

    def test_a_folder_of_another_workspace_is_rejected_as_not_found(self, authed_client, app_v2):
        from revoluzen_flow.main import app_state
        project_id = _project(authed_client, "Folder check: workspaces")
        foreign = Folder(name="Not yours", project_id=project_id, workspace_id="some-other-workspace")
        app_state["folder_store"].create(foreign)

        created = _create(authed_client, "Folder check: foreign folder", project_id=project_id, folder_id=foreign.id)

        # Same answer as for a folder that does not exist, so its existence does not leak.
        assert created.status_code == 404, created.text

    def test_no_folder_means_nothing_is_checked(self, authed_client):
        created = _create(authed_client, "Folder check: no folder")
        assert created.status_code == 200, created.text
        assert authed_client.get(f"/api/workflows/{created.json()['id']}").json()["workflow"]["folder_id"] in (None, "")


class TestDeletingAFolder:
    def _folder_with(self, authed_client, how_many: int):
        project_id = _project(authed_client, f"Folder delete: {how_many}")
        folder_id = _folder(authed_client, project_id, "Doomed")
        ids = [_make(authed_client, f"Folder delete: pipeline {i}", project_id=project_id, folder_id=folder_id)
               for i in range(how_many)]
        return project_id, folder_id, ids

    def test_the_preview_counts_the_pipelines_without_changing_anything(self, authed_client):
        _project_id, folder_id, ids = self._folder_with(authed_client, 2)

        preview = authed_client.get(f"/api/folders/{folder_id}/delete-preview")

        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["folder_name"] == "Doomed"
        assert body["affected_workflow_count"] == 2
        assert sorted(body["affected_workflow_ids"]) == sorted(ids)
        assert body["descendant_folder_ids"] == []
        assert authed_client.get(f"/api/folders/{folder_id}").status_code == 200

    def test_an_empty_folder_deletes_without_a_mode(self, authed_client):
        _project_id, folder_id, _ids = self._folder_with(authed_client, 0)

        deleted = authed_client.delete(f"/api/folders/{folder_id}")

        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["mode"] == "empty"
        assert authed_client.get(f"/api/folders/{folder_id}").status_code == 404

    def test_a_folder_with_pipelines_refuses_to_guess(self, authed_client):
        _project_id, folder_id, ids = self._folder_with(authed_client, 2)

        refused = authed_client.delete(f"/api/folders/{folder_id}")

        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["affected_workflow_count"] == 2
        # Nothing was touched.
        assert authed_client.get(f"/api/folders/{folder_id}").status_code == 200
        for wid in ids:
            assert authed_client.get(f"/api/workflows/{wid}").status_code == 200

    def test_move_to_root_keeps_the_pipelines(self, authed_client):
        _project_id, folder_id, ids = self._folder_with(authed_client, 2)

        deleted = authed_client.delete(f"/api/folders/{folder_id}", params={"move_to_root": "true"})

        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["moved_to_root_workflow_count"] == 2
        assert authed_client.get(f"/api/folders/{folder_id}").status_code == 404
        for wid in ids:
            wf = authed_client.get(f"/api/workflows/{wid}").json()["workflow"]
            assert wf["folder_id"] in (None, "")

    def test_force_sends_every_pipeline_to_the_bin_and_removes_the_folder(self, authed_client):
        _project_id, folder_id, ids = self._folder_with(authed_client, 2)

        deleted = authed_client.delete(f"/api/folders/{folder_id}", params={"force": "true"})

        assert deleted.status_code == 200, deleted.text
        body = deleted.json()
        assert body["mode"] == "force" and body["deleted_workflow_count"] == 2
        assert authed_client.get(f"/api/folders/{folder_id}").status_code == 404
        binned = {i["id"] for i in authed_client.get("/api/workflows/bin").json()["items"]}
        for wid in ids:
            assert authed_client.get(f"/api/workflows/{wid}").status_code == 404
            assert wid in binned
        for wid in ids:
            authed_client.delete(f"/api/workflows/bin/{wid}")

    def test_force_on_an_empty_folder_is_harmless(self, authed_client):
        _project_id, folder_id, _ids = self._folder_with(authed_client, 0)
        deleted = authed_client.delete(f"/api/folders/{folder_id}", params={"force": "true"})
        assert deleted.status_code == 200 and deleted.json()["deleted_workflow_count"] == 0


class TestPublishedPipelinesBlockAFolderDelete:
    """The Pipelines page offers Delete only for a draft, failed or archived
    pipeline: a published one has to be revoked first. A folder delete with
    ``force`` is a way to delete pipelines, so it follows the same rule instead of
    sending live pipelines to the Bin."""

    @staticmethod
    def _set_status(wid: str, status: PipelineStatus) -> None:
        from revoluzen_flow.main import app_state
        app_state["store"].update_status(wid, status)

    def _folder_with_a_live_and_a_draft(self, authed_client, label: str):
        project_id = _project(authed_client, f"Folder published: {label}")
        folder_id = _folder(authed_client, project_id, "Live folder")
        live = _make(authed_client, f"Folder published: {label} live", project_id=project_id, folder_id=folder_id)
        draft = _make(authed_client, f"Folder published: {label} draft", project_id=project_id, folder_id=folder_id)
        self._set_status(live, PipelineStatus.PUBLISHED)
        return folder_id, live, draft

    def _binned(self, authed_client) -> set[str]:
        return {i["id"] for i in authed_client.get("/api/workflows/bin").json()["items"]}

    def test_the_preview_names_the_published_pipelines(self, authed_client):
        folder_id, live, _draft = self._folder_with_a_live_and_a_draft(authed_client, "preview")

        body = authed_client.get(f"/api/folders/{folder_id}/delete-preview").json()

        assert body["affected_workflow_count"] == 2
        assert body["published_workflow_count"] == 1
        assert body["published_workflow_ids"] == [live]
        assert body["published_workflow_names"] == ["Folder published: preview live"]

    def test_the_preview_reports_none_when_nothing_is_published(self, authed_client):
        project_id = _project(authed_client, "Folder published: none")
        folder_id = _folder(authed_client, project_id)
        _make(authed_client, "Folder published: none draft", project_id=project_id, folder_id=folder_id)

        body = authed_client.get(f"/api/folders/{folder_id}/delete-preview").json()

        assert body["published_workflow_count"] == 0
        assert body["published_workflow_ids"] == [] and body["published_workflow_names"] == []

    def test_force_is_refused_while_one_is_published_and_nothing_changes(self, authed_client):
        folder_id, live, draft = self._folder_with_a_live_and_a_draft(authed_client, "refuse")

        refused = authed_client.delete(f"/api/folders/{folder_id}", params={"force": "true"})

        assert refused.status_code == 409, refused.text
        detail = refused.json()["detail"]
        assert detail["code"] == "published_pipelines_in_folder"
        assert detail["published_workflow_count"] == 1 and detail["published_workflow_ids"] == [live]
        assert detail["published_workflow_names"] == ["Folder published: refuse live"]
        assert '"Folder published: refuse live"' in detail["message"] and "unpublish" in detail["message"]
        # All or nothing: the draft was not sent to the Bin either, and the folder is still there.
        assert authed_client.get(f"/api/folders/{folder_id}").status_code == 200
        for wid in (live, draft):
            assert authed_client.get(f"/api/workflows/{wid}").status_code == 200
        assert not ({live, draft} & self._binned(authed_client))

    def test_the_refusal_names_a_few_and_counts_the_rest(self, authed_client):
        project_id = _project(authed_client, "Folder published: many")
        folder_id = _folder(authed_client, project_id)
        for i in range(5):
            wid = _make(authed_client, f"Folder published: many {i}", project_id=project_id, folder_id=folder_id)
            self._set_status(wid, PipelineStatus.PUBLISHED)

        detail = authed_client.delete(f"/api/folders/{folder_id}", params={"force": "true"}).json()["detail"]

        assert detail["published_workflow_count"] == 5
        assert "5 published pipelines" in detail["message"] and "and 2 more" in detail["message"]

    def test_once_it_is_revoked_the_delete_goes_through(self, authed_client):
        folder_id, live, draft = self._folder_with_a_live_and_a_draft(authed_client, "revoke")
        assert authed_client.delete(f"/api/folders/{folder_id}", params={"force": "true"}).status_code == 409

        revoked = authed_client.post(f"/api/workflows/{live}/revoke")
        assert revoked.status_code == 200, revoked.text
        deleted = authed_client.delete(f"/api/folders/{folder_id}", params={"force": "true"})

        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["deleted_workflow_count"] == 2
        assert {live, draft} <= self._binned(authed_client)
        for wid in (live, draft):
            authed_client.delete(f"/api/workflows/bin/{wid}")

    def test_move_to_root_is_not_blocked_and_keeps_the_pipeline_published(self, authed_client):
        folder_id, live, draft = self._folder_with_a_live_and_a_draft(authed_client, "move")

        deleted = authed_client.delete(f"/api/folders/{folder_id}", params={"move_to_root": "true"})

        assert deleted.status_code == 200, deleted.text
        assert authed_client.get(f"/api/folders/{folder_id}").status_code == 404
        kept = authed_client.get(f"/api/workflows/{live}").json()["workflow"]
        assert kept["folder_id"] in (None, "") and kept["status"] == "published"
        assert authed_client.get(f"/api/workflows/{draft}").status_code == 200

    def test_draft_failed_and_archived_pipelines_do_not_block(self, authed_client):
        project_id = _project(authed_client, "Folder published: others")
        folder_id = _folder(authed_client, project_id)
        ids = [_make(authed_client, f"Folder published: other {i}", project_id=project_id, folder_id=folder_id)
               for i in range(3)]
        self._set_status(ids[1], PipelineStatus.FAILED)
        self._set_status(ids[2], PipelineStatus.ARCHIVED)

        deleted = authed_client.delete(f"/api/folders/{folder_id}", params={"force": "true"})

        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["deleted_workflow_count"] == 3
        for wid in ids:
            authed_client.delete(f"/api/workflows/bin/{wid}")


class TestWhoMayDeleteAFolder:
    """Deleting a pipeline takes developer rank (analyst and viewer get 403), and a
    folder delete can delete the pipelines in it, so it takes the same rank.
    It used to ask only for a signed-in user."""

    def _folder_with_one(self, authed_client):
        project_id = _project(authed_client, "Folder rank")
        folder_id = _folder(authed_client, project_id, "Guarded")
        wid = _make(authed_client, "Folder rank: pipeline", project_id=project_id, folder_id=folder_id)
        return folder_id, wid

    @pytest.mark.parametrize("role", ["viewer", "analyst"])
    def test_a_read_only_role_cannot_delete_a_folder_or_send_its_pipelines_to_the_bin(
        self, authed_client, role_clients, role,
    ):
        client = role_clients.get(role)
        if client is None:
            pytest.skip(f"{role} client not provisioned")
        folder_id, wid = self._folder_with_one(authed_client)

        for params in ({}, {"force": "true"}, {"move_to_root": "true"}):
            assert client.delete(f"/api/folders/{folder_id}", params=params).status_code == 403

        # Nothing changed.
        assert authed_client.get(f"/api/folders/{folder_id}").status_code == 200
        assert authed_client.get(f"/api/workflows/{wid}").status_code == 200

    def test_a_data_engineer_can(self, authed_client, role_clients):
        engineer = role_clients.get("data_engineer")
        if engineer is None:
            pytest.skip("data_engineer client not provisioned")
        folder_id, wid = self._folder_with_one(authed_client)

        deleted = engineer.delete(f"/api/folders/{folder_id}", params={"force": "true"})

        assert deleted.status_code == 200, deleted.text
        assert authed_client.get(f"/api/workflows/{wid}").status_code == 404
        authed_client.delete(f"/api/workflows/bin/{wid}")

    def test_everyone_signed_in_can_still_look_at_the_preview(self, authed_client, role_clients):
        viewer = role_clients.get("viewer")
        if viewer is None:
            pytest.skip("viewer client not provisioned")
        folder_id, _wid = self._folder_with_one(authed_client)
        assert viewer.get(f"/api/folders/{folder_id}/delete-preview").status_code == 200
