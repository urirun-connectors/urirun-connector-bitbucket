"""Bitbucket Cloud adapter for the provider-neutral forge contract."""

from __future__ import annotations

import importlib
import os
import urllib.parse
from importlib.metadata import version
from typing import Any

import urirun
from urirun_connector_forge import ForgeHttpClient, operation_receipt, repository_identity, require_ref, require_sha

CONNECTOR_ID = "bitbucket"
_make_connector = getattr(urirun, "connector", None) or importlib.import_module("urirun._connector").connector
_load_manifest = getattr(urirun, "load_manifest", None) or importlib.import_module("urirun._connector").load_manifest
conn = _make_connector(CONNECTOR_ID, scheme="bitbucket")


def _client() -> ForgeHttpClient:
    reference = os.environ.get("BITBUCKET_TOKEN_REF", "getv://BITBUCKET_TOKEN").strip()
    if not reference.startswith(("getv://", "secret://", "{getv:", "{secret:")):
        raise RuntimeError("bitbucket_token_ref_invalid")
    token = urirun.resolve_secret(reference, secret_allow=os.environ.get("URIRUN_SECRET_ALLOW", reference))
    if not token:
        raise RuntimeError("bitbucket_auth_unavailable")
    return ForgeHttpClient("https://api.bitbucket.org/2.0", {"Authorization": f"Bearer {token}"})


def _repo(workspace: str, repository: str) -> str:
    repository_identity(workspace, repository)
    return f"/repositories/{urllib.parse.quote(workspace, safe='')}/{urllib.parse.quote(repository, safe='')}"


def _inputs(branch: str, expected_head: str, idempotency_key: str) -> tuple[str, str]:
    if not idempotency_key:
        raise ValueError("forge_idempotency_key_required")
    return require_ref(branch), require_sha(expected_head)


def _branch(client: ForgeHttpClient, base: str, branch: str) -> tuple[int, Any]:
    status, payload, _ = client.request(
        "GET", f"{base}/refs/branches/{urllib.parse.quote(branch, safe='')}", expected=(200, 404)
    )
    return status, payload


@conn.handler("auth/query/status", isolated=True, meta={"label": "Bitbucket authentication status"})
def auth_status() -> dict[str, Any]:
    try:
        _, user, _ = _client().request("GET", "/user")
        return urirun.ok(authenticated=True, provider="bitbucket", username=user.get("username", ""))
    except (RuntimeError, ValueError) as error:
        return urirun.fail(str(error), authenticated=False, provider="bitbucket")


@conn.handler("repository/query/snapshot", isolated=True, meta={"label": "Bitbucket repository snapshot"})
def repository_snapshot(workspace: str = "", repository: str = "") -> dict[str, Any]:
    try:
        base = _repo(workspace, repository)
        client = _client()
        _, metadata, _ = client.request("GET", base)
        _, branch_page, _ = client.request("GET", f"{base}/refs/branches", query={"pagelen": 100})
        _, change_page, _ = client.request("GET", f"{base}/pullrequests", query={"state": "OPEN", "pagelen": 100})
    except (RuntimeError, ValueError) as error:
        return urirun.fail(str(error), provider="bitbucket")
    branches = branch_page.get("values", [])
    changes = change_page.get("values", [])
    return urirun.ok(
        schema="urirun.forge-repository-snapshot/v1", provider="bitbucket",
        repository=f"{workspace}/{repository}", default_branch=metadata.get("mainbranch", {}).get("name", ""),
        branches=[{
            "name": row.get("name", ""), "head_sha": row.get("target", {}).get("hash", ""),
            "merged": False, "protected": False,
        } for row in branches],
        change_requests=[{
            "number": row.get("id"), "title": row.get("title", ""),
            "head_branch": row.get("source", {}).get("branch", {}).get("name", ""),
            "head_sha": row.get("source", {}).get("commit", {}).get("hash", ""),
            "base_branch": row.get("destination", {}).get("branch", {}).get("name", ""),
            "draft": False, "mergeability": "unknown", "check_state": row.get("state", "unknown").lower(),
            "url": row.get("links", {}).get("html", {}).get("href", ""),
        } for row in changes],
        complete=not branch_page.get("next") and not change_page.get("next"),
    )


@conn.handler("change-request/command/close", isolated=True, meta={"label": "Decline Bitbucket pull request"})
def close_change_request(workspace: str = "", repository: str = "", number: int = 0,
                         expected_head: str = "", idempotency_key: str = "") -> dict[str, Any]:
    try:
        head = require_sha(expected_head)
        if int(number) < 1 or not idempotency_key:
            raise ValueError("forge_change_request_input_invalid")
        base = _repo(workspace, repository)
        client = _client()
        path = f"{base}/pullrequests/{int(number)}"
        _, before, _ = client.request("GET", path)
        if before.get("source", {}).get("commit", {}).get("hash") != head:
            raise ValueError("forge_expected_head_changed")
        already = before.get("state") == "DECLINED"
        if not already:
            client.request("POST", f"{path}/decline", expected=(200,))
        _, after, _ = client.request("GET", path)
        if after.get("state") != "DECLINED":
            raise RuntimeError("forge_readback_failed")
        return urirun.ok(**operation_receipt(
            provider="bitbucket", operation="forge.change_request.close",
            repository=f"{workspace}/{repository}", expected_head=head,
            observed_before=str(before.get("state", "unknown")).lower(), observed_after="declined",
            idempotency_key=idempotency_key, evidence_uri="bitbucket://host/change-request/query/state",
            status="already_satisfied" if already else "validated",
        ))
    except (RuntimeError, ValueError) as error:
        return urirun.fail(str(error), provider="bitbucket")


@conn.handler("branch/command/archive", isolated=True, meta={"label": "Archive Bitbucket branch as tag"})
def archive_branch(workspace: str = "", repository: str = "", branch: str = "", expected_head: str = "",
                   tag: str = "", idempotency_key: str = "") -> dict[str, Any]:
    try:
        branch, head = _inputs(branch, expected_head, idempotency_key)
        tag = require_ref(tag)
        base = _repo(workspace, repository)
        client = _client()
        status, current = _branch(client, base, branch)
        if status != 200 or current.get("target", {}).get("hash") != head:
            raise ValueError("forge_expected_head_changed")
        tag_path = f"{base}/refs/tags/{urllib.parse.quote(tag, safe='')}"
        tag_status, existing, _ = client.request("GET", tag_path, expected=(200, 404))
        already = tag_status == 200
        if already and existing.get("target", {}).get("hash") != head:
            raise ValueError("forge_archive_conflict")
        if not already:
            client.request("POST", f"{base}/refs/tags", body={"name": tag, "target": {"hash": head}}, expected=(201,))
        _, verified, _ = client.request("GET", tag_path)
        if verified.get("target", {}).get("hash") != head:
            raise RuntimeError("forge_readback_failed")
        return urirun.ok(**operation_receipt(
            provider="bitbucket", operation="forge.branch.archive", repository=f"{workspace}/{repository}",
            expected_head=head, observed_before="tag_present" if already else "tag_absent",
            observed_after="tag_present", idempotency_key=idempotency_key,
            evidence_uri="bitbucket://host/tag/query/ref",
            status="already_satisfied" if already else "validated",
        ))
    except (RuntimeError, ValueError) as error:
        return urirun.fail(str(error), provider="bitbucket")


@conn.handler("branch/command/delete", isolated=True, meta={"label": "Delete Bitbucket branch"})
def delete_branch(workspace: str = "", repository: str = "", branch: str = "", expected_head: str = "",
                  idempotency_key: str = "") -> dict[str, Any]:
    try:
        branch, head = _inputs(branch, expected_head, idempotency_key)
        base = _repo(workspace, repository)
        client = _client()
        status, current = _branch(client, base, branch)
        already = status == 404
        if not already:
            if current.get("target", {}).get("hash") != head:
                raise ValueError("forge_expected_head_changed")
            path = f"{base}/refs/branches/{urllib.parse.quote(branch, safe='')}"
            client.request("DELETE", path, expected=(204,))
        verified, _ = _branch(client, base, branch)
        if verified != 404:
            raise RuntimeError("forge_readback_failed")
        return urirun.ok(**operation_receipt(
            provider="bitbucket", operation="forge.branch.delete", repository=f"{workspace}/{repository}",
            expected_head=head, observed_before="absent" if already else "present", observed_after="absent",
            idempotency_key=idempotency_key, evidence_uri="bitbucket://host/branch/query/ref",
            status="already_satisfied" if already else "validated",
        ))
    except (RuntimeError, ValueError) as error:
        return urirun.fail(str(error), provider="bitbucket")


@conn.handler("branch/command/restore", isolated=True, meta={"label": "Restore Bitbucket branch"})
def restore_branch(workspace: str = "", repository: str = "", branch: str = "", expected_head: str = "",
                   tag: str = "", idempotency_key: str = "") -> dict[str, Any]:
    try:
        branch, head = _inputs(branch, expected_head, idempotency_key)
        tag = require_ref(tag)
        base = _repo(workspace, repository)
        client = _client()
        _, archived, _ = client.request("GET", f"{base}/refs/tags/{urllib.parse.quote(tag, safe='')}")
        if archived.get("target", {}).get("hash") != head:
            raise ValueError("forge_archive_head_changed")
        status, current = _branch(client, base, branch)
        already = status == 200
        if already and current.get("target", {}).get("hash") != head:
            raise ValueError("forge_restore_conflict")
        if not already:
            client.request("POST", f"{base}/refs/branches", body={"name": branch, "target": {"hash": head}}, expected=(201,))
        _, verified = _branch(client, base, branch)
        if verified.get("target", {}).get("hash") != head:
            raise RuntimeError("forge_readback_failed")
        return urirun.ok(**operation_receipt(
            provider="bitbucket", operation="forge.branch.restore", repository=f"{workspace}/{repository}",
            expected_head=head, observed_before="present" if already else "absent", observed_after="present",
            idempotency_key=idempotency_key, evidence_uri="bitbucket://host/branch/query/ref",
            status="already_satisfied" if already else "validated",
        ))
    except (RuntimeError, ValueError) as error:
        return urirun.fail(str(error), provider="bitbucket")


@conn.handler("doctor/query/report", isolated=True, meta={"label": "Bitbucket connector readiness"})
def doctor() -> dict[str, Any]:
    return urirun.ok(connector=CONNECTOR_ID, version=_version(), status="ready", implementations=["bitbucket-cloud"])


def _version() -> str:
    try:
        return version("urirun-connector-bitbucket")
    except Exception:
        return "0.1.0"


def urirun_bindings() -> dict[str, Any]:
    return conn.bindings()


def connector_manifest() -> dict[str, Any]:
    return conn.manifest(_load_manifest(__package__))


def main(argv: list[str] | None = None) -> int:
    return conn.cli(argv, manifest_prose=_load_manifest(__package__))
