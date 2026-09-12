#!/usr/bin/env python3
"""Prepare a truthful update to the original handoff; only Library CAS persistence makes it effective."""
import argparse
import copy
import json
from pathlib import Path
import os

import collector as c
from short_term import digest, write_json
import trade_calendar

def run(handoff,expected,quality,model,task_state,output):
    if not handoff.is_file() or digest(handoff)!=expected:
        raise ValueError("original handoff hash changed or missing; restore current version first")
    original=json.loads(handoff.read_text())
    q=json.loads(quality.read_text())
    m=json.loads(model.read_text()) if model and model.is_file() else {"status":"not_computed","model_ready":False}
    tasks=json.loads(task_state.read_text())
    if q["kind"]!="full_history_replay_acceptance" or q.get("production_database_written") is not False:
        raise ValueError("unexpected cloud evidence contract")
    if c.timestamp(q["generated_at"])>c.now():
        raise ValueError("future evidence timestamp")
    if tasks.get("verified_at") is None or c.timestamp(tasks["verified_at"])>c.now():
        raise ValueError("missing actual automation verification time")
    current=copy.deepcopy(original)
    previous=current.get("latest_attempt")
    if previous:current.setdefault("previous_attempts",[]).append(previous)
    db=current["resources"].get("latest_market_database") or previous["market_db"]
    end=q["target_date"]
    next_day=trade_calendar.next_session(end)
    stamp=c.stamp()
    blockers=["云端回填证据尚未合并并同身份保存到当前原行情库",
              "官方全市场历史主表/状态/逐日限价和时点复权仍不足",
              "完整行业、新闻事件和可成交条件单证据仍不足",
              "探索性P1/P2时间外结果不等于正式四概率模型或80%净盈利验证"]
    current.update(status="DATA NOT READY",phase="cloud_history_verified_import_pending",
        generated_at=stamp,bootstrap_complete=False,model_ready=False,next_trade_date=next_day,
        blocking_reasons=blockers)
    current["latest_attempt"]={"kind":"cloud_full_history_replay_and_model_diagnostic",
        "target_date_checked":end,"generated_at":stamp,"data_status":"DATA NOT READY",
        "bootstrap_complete":False,"model_ready":False,"market_db":db,
        "cloud_evidence_not_yet_imported":True,"observed_base_date":end,
        "cloud_verified_history_symbols":q["verified_history_symbols"],
        "cloud_history_250_symbols":q["history_250_symbols"],
        "cloud_history_250_latest_at_target":q["history_250_latest_at_target"],
        "cloud_total_real_bars":q["total_real_bars"],
        "vendor_expected_securities":q["vendor_universe"],"expected_securities":None,
        "source_run_url":"https://github.com/cluuacc-gif/ashare-research/actions/runs/"+str(q["github_run_id"]),
        "quality_sha256":digest(quality),"diagnostic_model_status":m.get("status"),
        "formal_predictions_created":0,"original_market_database_unchanged":True,
        "prediction_database_touched":False,"reason":blockers}
    current["resources"]["market_database"].update(
        latest_observed_version=db["version"],latest_observed_sha256=db["sha256"])
    current["resources"]["current_source_repository"].update(
        url="https://github.com/cluuacc-gif/ashare-research",
        commit_sha=q["commit_sha"],model_status=m.get("status","not_computed"),
        formal_model_ready=False)
    current["resources"]["rules"]["workflow_version"]="1.2"
    current["evening_task_state"]={**tasks["evening"],"updated_at":tasks["verified_at"]}
    current["morning_task_state"]={**tasks["morning"],"updated_at":tasks["verified_at"]}
    current["calendar_control"]={"verified_next_session":next_day,"based_on":end,
        "config_sha256":digest(Path(trade_calendar.__file__).parent/"exchange_calendar_2026.json"),
        "scope":"2026 documented exchange holidays; not a historical data completeness claim"}
    current["handoff_save_status"]="prepared_for_same_original_library_id_replacement"
    # Success pointers and all old production versions are preserved verbatim.
    if current.get("latest_successful_dataset")!=original.get("latest_successful_dataset"):
        raise ValueError("success pointer changed unexpectedly")
    if digest(handoff)!=expected:
        raise ValueError("original handoff changed during preparation")
    write_json(output,current)
    print(json.dumps({"prepared_path":str(output),"sha256":digest(output),
        "same_identity_persistence_pending":True,"next_session":next_day,"data_status":"DATA NOT READY"}))

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--handoff",type=Path,required=True);p.add_argument("--expected-handoff-sha256",required=True)
    p.add_argument("--quality",type=Path,required=True);p.add_argument("--model",type=Path)
    p.add_argument("--task-state",type=Path,required=True);p.add_argument("--output",type=Path,required=True)
    a=p.parse_args();run(a.handoff,a.expected_handoff_sha256,a.quality,a.model,a.task_state,a.output)
