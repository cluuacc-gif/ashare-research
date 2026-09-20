#!/usr/bin/env python3
"""Durably publish verified public quote artifacts, never a production DB.

The data/evening branch is an append-only transport index, not the canonical
Library handoff and not a DATA READY assertion. All original bytes survive.
"""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request
import zipfile

from import_probe_artifact import verify


def sha(body):
    return hashlib.sha256(body).hexdigest()


def run(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def publish(archive, root, source_run, original_digest=None):
    report, _, securities, quotes = verify(archive)
    source_run = str(source_run)
    if not re.fullmatch(r"[0-9]+", source_run):
        raise ValueError("invalid source run")
    if str(report.get("github_run_id")) != source_run:
        raise ValueError("source run identity mismatch")
    body = Path(archive).read_bytes()
    if original_digest and sha(body) != original_digest.removeprefix("sha256:"):
        raise ValueError("GitHub artifact digest mismatch")
    day = report["calendar_upper_bound"]
    key = day+"/"+source_run+"-"+sha(body)[:16]
    target = Path(root)/"bundles"/key
    entries = []
    target.mkdir(parents=True, exist_ok=True)
    for i, start in enumerate(range(0, len(body), 512*1024)):
        block = body[start:start+512*1024]
        path = target/f"{i:04d}.b64"
        encoded = base64.b64encode(block)+b"\n"
        if path.exists() and path.read_bytes() != encoded:
            raise ValueError("immutable transport conflict")
        path.write_bytes(encoded)
        entries.append({"path": str(path.relative_to(root)), "bytes":len(block), "sha256":sha(block)})
    summary = {"schema_version":"1.0", "kind":"verified_public_quote_transport",
               "base_trade_date":day,"github_run_id":source_run,
               "github_commit":report.get("github_commit"),
               "archive_sha256":sha(body),"archive_bytes":len(body),
               "collector_sha256":report["collector_sha256"],
               "original_finished_at":report["finished_at"],
               "published_at":dt.datetime.now(dt.timezone.utc).isoformat(),
               "raw_replay_verified":True,"securities":len(securities),
               "quotes":len(quotes),
               "quote_exchange_counts":{ex:sum(q["symbol"].endswith(ex) for q in quotes) for ex in ("SH","SZ","BJ")},
               "data_status":"DATA NOT READY", "model_ready":False,
               "canonical_database_imported":False,"files":entries}
    index=target/"index.json"
    if not index.exists():
        index.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
    else:
        old=json.loads(index.read_text())
        if old["archive_sha256"]!=summary["archive_sha256"]:
            raise ValueError("immutable index conflict")
        summary=old
    catalog_path=Path(root)/"catalog.json"
    catalog=json.loads(catalog_path.read_text()) if catalog_path.exists() else {"schema_version":"1.0","datasets":[]}
    item={k:v for k,v in summary.items() if k!="files"}
    item["index_path"]=str(index.relative_to(root))
    if not any(x["index_path"]==item["index_path"] for x in catalog["datasets"]):
        catalog["datasets"].append(item)
    catalog["datasets"].sort(key=lambda x:(x["base_trade_date"],x["original_finished_at"],x["index_path"]))
    catalog_path.write_text(json.dumps(catalog,ensure_ascii=False,indent=2)+"\n")
    (Path(root)/"latest.json").write_text(json.dumps(catalog["datasets"][-1],ensure_ascii=False,indent=2)+"\n")
    return item


def json_api(path):
    repository=os.environ["GITHUB_REPOSITORY"]
    request=urllib.request.Request("https://api.github.com/repos/"+repository+"/"+path,
        headers={"Authorization":"Bearer "+os.environ["GH_TOKEN"],"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"})
    with urllib.request.urlopen(request,timeout=45) as response:
        return json.load(response)


def recover(request_file, root, working):
    request=json.loads(Path(request_file).read_text())
    if request.get("operation")!="recover_existing_evening_artifacts" or len(request.get("runs",[]))>10:
        raise ValueError("invalid bounded recovery request")
    output=[]
    for entry in request["runs"]:
        rid=int(entry["run_id"])
        meta=json_api(f"actions/runs/{rid}")
        if meta["path"]!=".github/workflows/daily-data.yml" or meta["status"]!="completed":
            raise ValueError("not completed daily collection")
        artifacts=json_api(f"actions/runs/{rid}/artifacts")["artifacts"]
        matches=[a for a in artifacts if a["id"]==int(entry["artifact_id"]) and not a["expired"]]
        if len(matches)!=1: raise ValueError("artifact absent or expired")
        artifact=matches[0]
        archive=Path(working)/f"{rid}.zip"
        # gh supplies the existing repository authorization; no user secrets.
        with archive.open("wb") as stream:
            subprocess.run(["gh","api",f"repos/{os.environ['GITHUB_REPOSITORY']}/actions/artifacts/{artifact['id']}/zip"],stdout=stream,check=True)
        item=publish(archive,root,rid,artifact.get("digest"))
        if item["base_trade_date"]!=entry["expected_date"]:
            raise ValueError("recovered date differs from request")
        output.append(item)
    return output


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir",type=Path)
    parser.add_argument("--recover-request",type=Path)
    parser.add_argument("--data-root",required=True,type=Path)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory() as working:
        if args.recover_request:
            result=recover(args.recover_request,args.data_root,working)
        else:
            if not args.source_dir or not (args.source_dir/"source_acceptance.json").is_file():
                print(json.dumps({"status":"no_quote_artifact_to_publish"}));return
            archive=Path(working)/"evening.zip"
            with zipfile.ZipFile(archive,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
                for path in sorted(args.source_dir.rglob("*")):
                    if path.is_file():
                        if path.is_symlink() or path.suffix in (".sqlite3",".db"):
                            raise ValueError("production/private files forbidden in transport")
                        z.write(path,path.relative_to(args.source_dir))
            result=publish(archive,args.data_root,os.environ["GITHUB_RUN_ID"])
        print(json.dumps(result,ensure_ascii=False))


if __name__=="__main__":main()
