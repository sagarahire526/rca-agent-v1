"""
One-shot seeder: writes the 5 client-shared RCA scenarios (from PDF) into
the internal scenarios store with embeddings.

Idempotent on `tag` — re-running skips scenarios whose tag is already present.
Pass `--replace` to delete existing entries with the same tag and re-insert.

Usage (from the app/ directory):
    ../venv/bin/python scripts/seed_internal_scenarios.py
    # or to overwrite existing entries:
    ../venv/bin/python scripts/seed_internal_scenarios.py --replace
"""
from __future__ import annotations

import argparse
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

from services.internal_scenarios import get_internal_scenarios_store


SCENARIOS = [
    {
        "tag": "Cx start delay RCA — month-wise",
        "question": (
            "How many sites Cx start (Swap start) delayed in last 6 months. "
            "Provide Month wise delayed site count with RCA summary"
        ),
        "steps": [
            "For Swap completed sites - Retrieve planned vs actual Cx start dates and compute delays",
            "Identify delayed sites and aggregate month-wise counts",
            "For not completed sites, identify if any Start delay code is there",
            "Break delays by dependency type (material, Access, crew, etc.)",
            "Identify dominant delay drivers per month",
            "Check recurring vs one-time issues",
            "Map delays to vendors/GCs if needed & Provide month-wise RCA summary",
        ],
    },
    {
        "tag": "GC punch points >15 — RCA + top issue",
        "question": (
            "Identify the GCs with average punch point per site is >15 & "
            "provide GC wise RCA & top-1 repeated punch point against each GC"
        ),
        "steps": [
            "Calculate average punch points per site for each GC",
            "Identify GCs with average >15 and rank them",
            "Break punch points into categories",
            "Identify most repeated punch point per GC",
            "Check recurrence across sites",
            "Analyse cause (Crew process adherence, Execution issues)",
            "Provide GC-wise RCA with top issue",
        ],
    },
    {
        "tag": "Region forecast vs actual accuracy — RCA <90%",
        "question": (
            "Find last 3 months region wise forecast vs actual accuracy & "
            "provide region wise RCA for <90% accuracy cases"
        ),
        "steps": [
            "Compute forecast vs actual accuracy per region",
            "Identify regions with accuracy <90%",
            "Measure over/under forecasting trend",
            "Break variance by drivers (readiness, capacity, delays)",
            "Identify planning vs execution gaps",
            "Provide region-wise RCA",
        ],
    },
    {
        "tag": "GC material-to-closeout lead time — top 5 RCA",
        "question": (
            "Which GCs are having highest Material to Site closeout lead time? "
            "Pick top-5 GCs & Provide the RCA for them"
        ),
        "steps": [
            "Calculate material-to-closeout lead time per GC",
            "Rank GCs and select top 5 with highest delays",
            "Break lead time into stages to identify delay area",
            "Identify whether delay is due to on-hold, execution driven, dependency related",
            "Check recurring patterns across sites",
            "Provide RCA for each GC (Top-5)",
        ],
    },
    {
        "tag": "Rev rec run rate — bottom 5 markets RCA",
        "question": (
            "Provide last 6 weeks rev. rec run rate & analyse which market has low "
            "run rate. Provide market wise RCA summary for bottom 5 markets"
        ),
        "steps": [
            "Calculate weekly revenue recognition run rate per market",
            "Identify bottom 5 markets with lowest performance",
            "Analyse milestone progression delays",
            "Break down bottlenecks by dependency or execution phase (Installation, SCOP submission & Acceptance)",
            "Compare with historical performance",
            "Identify key limiting factors (Vendor delay, Customer approval delay, Revisit etc)",
            "Provide market-wise RCA summary",
        ],
    },
    {
        "tag": "Rev rec prioritization — next 3 weeks",
        "question": (
            "Which sites should we prioritize to maximize the Rev. rec rate in next 3 weeks?"
        ),
        "steps": [
            "Identify sites nearing revenue milestones within next 3 weeks",
            "Check current progress and remaining activities for each site",
            "Filter sites with minimal pending dependencies",
            "Compare expected closure speed using recent performance trends",
            "Rank sites by fastest revenue realization potential",
            "Recommend priority list with targeted actions to unlock milestones",
        ],
    },
    {
        "tag": "Low performing markets — cycle time / SCOP FTR / PP",
        "question": (
            "Identify low performing markets in terms of Cycle time, SCOP FTR, Average PP "
            "per site & provide the improvement suggestions for top 2 low performing "
            "markets in each region"
        ),
        "steps": [
            "Evaluate market-wise performance across cycle time (Installation, SCOP Submission & E2E), FTR and punch points",
            "Rank markets within each region to identify lowest performers",
            "Select top 2 weakest markets per region",
            "Identify which metric is driving underperformance in each market",
            "Trace issues to vendors, crews or recurring defects",
            "Compare with better-performing markets to identify gaps",
            "Recommend targeted improvement actions",
        ],
    },
    {
        "tag": "Forecast accuracy improvement — next 4 weeks",
        "question": (
            "How to improve forecast accuracy for the sites planned in next 4 weeks?"
        ),
        "steps": [
            "Compare recent (Past 12 weeks or 90 days) forecast vs actual to identify deviation patterns",
            "Identify main drivers of inaccuracy (readiness gaps, delays, capacity issues)",
            "Evaluate readiness and dependency certainty for upcoming sites (Next 4 weeks planned)",
            "Flag sites with high uncertainty or variability based on the current status & historical trend",
            "Adjust forecast inputs using historical reliability trends",
            "Recommend corrections for achieving ~99% accuracy & the focus areas",
        ],
    },
    {
        "tag": "Risk sites — planned next 4 weeks",
        "question": (
            "For sites planned in the next 4 weeks, which ones are at risk & where "
            "should I focus?"
        ),
        "steps": [
            "Identify all sites planned for next 4 weeks",
            "Assess prerequisite readiness and current status",
            "Detect missing or delayed dependencies",
            "Compare with historical delay patterns for similar conditions",
            "Flag high-risk sites in terms of dependency, readiness, Possible SCOP rejections, revisits & rollout delays and categorize by severity",
            "Highlight key focus areas with required interventions",
        ],
    },
    {
        "tag": "Installation SLA risk — material received",
        "question": (
            "Which sites are likely to exceed Installation SLA timelines where material "
            "received, and what corrective actions are recommended?"
        ),
        "steps": [
            "Identify sites where material is already received but installation not completed",
            "Measure installation SLA threshold",
            "Flag sites approaching or exceeding SLA risk",
            "Analyse delays after material readiness (crew, scheduling, access)",
            "Compare with historical installation timelines",
            "Recommend corrective actions (crew allocation, prioritization, escalation)",
        ],
    },
    {
        "tag": "Final acceptance rejection risk",
        "question": (
            "Which sites nearing final acceptance are at risk of rejection based on "
            "historical acceptance failures? Where do we focus"
        ),
        "steps": [
            "Identify sites nearing final acceptance (Installation, SCOP submission completed)",
            "Assess current quality indicators (punch points, failures patterns, rework)",
            "Compare with historical rejection patterns",
            "Flag sites with similar risk signals",
            "Identify specific quality gaps or pending issues",
            "Recommend targeted quality interventions",
        ],
    },
    {
        "tag": "Vendor over-allocation — site reassignment",
        "question": (
            "Which vendors are currently over-allocated beyond optimal capacity, and "
            "which sites should be reassigned to prevent delays"
        ),
        "steps": [
            "Evaluate vendor-wise workload against available capacity",
            "Identify vendors exceeding optimal capacity",
            "Detect sites at risk under overloaded vendors",
            "Identify alternate vendors with available capacity & performance",
            "Validate feasibility of reassignment",
            "Recommend site redistribution plan",
        ],
    },
    {
        "tag": "SCOP rejection reduction — upcoming sites",
        "question": (
            "Recommend the steps to reduce SCOP rejections in the upcoming sites based "
            "on the SCOP rejections from customer in last 90 days"
        ),
        "steps": [
            "Retrieve last 90 days SCOP rejection data with rejection reasons and site details",
            "Identify most frequent rejection categories and recurring PPs",
            "Analyse which vendors/GCs and work areas are contributing most to rejections",
            "Compare rejected vs successfully accepted sites to identify gaps",
            "Map key failure patterns to upcoming planned sites with similar conditions",
            "Recommend targeted actions such as focused QA checks, pre-validation, and vendor training on critical areas",
        ],
    },
    {
        "tag": "Material-to-On-Air backlog — market focus",
        "question": (
            "Which markets have the largest Material-to-On-Air backlog, and which "
            "dependency is limiting throughput & where should the PM focus to improve this?"
        ),
        "steps": [
            "Identify sites stuck between material pick-up and On-Air stage across markets",
            "Calculate backlog volume and aging for each market",
            "Rank markets based on backlog size and delay severity",
            "Break down pending sites by dependency (Crane, Access, On-hold, SCOP submission, Customer review etc)",
            "Identify the dominant bottleneck limiting throughput in each market",
            "Validate if constraint is capacity-driven or dependency-driven",
            "Highlight priority markets and recommend focused actions to unblock throughput",
        ],
    },
    {
        "tag": "Rescheduling frequency reduction — next 3 months",
        "question": (
            "Based on historical rescheduling patterns and current project status, what "
            "actionable steps can be implemented to minimize rescheduling frequency over "
            "the next three months?"
        ),
        "steps": [
            "Retrieve historical rescheduling data to identify frequency, timing and impacted stages",
            "Analyse patterns to determine primary drivers (prerequisite delays, capacity gaps, dependency volatility, etc.)",
            "Assess current project pipeline to identify sites with similar risk conditions",
            "Evaluate readiness stability and dependency certainty for upcoming planned sites",
            "Identify segments of plan with high likelihood of change or slippage",
            "Compare planned vs actual execution reliability trends to detect weak planning assumptions",
            "Recommend targeted actions such as stricter readiness gating, buffer-based scheduling, improved dependency tracking, and selective overbooking of low-risk sites",
            "Highlight specific areas (markets, vendors, stages) where tighter control will reduce rescheduling frequency",
        ],
    },
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Delete existing entries with the same tag and re-insert.",
    )
    args = parser.parse_args()

    store = get_internal_scenarios_store()
    existing = store.list_all()
    existing_by_tag: dict[str, str] = {s["tag"]: s["id"] for s in existing}

    print(f"Store currently has {len(existing)} scenario(s).\n")

    created = 0
    skipped = 0
    replaced = 0
    failed = 0

    for sc in SCENARIOS:
        tag = sc["tag"]
        if tag in existing_by_tag:
            if args.replace:
                old_id = existing_by_tag[tag]
                store.delete(old_id)
                print(f"  [REPLACE] Deleted existing '{tag}' (id={old_id})")
                replaced += 1
            else:
                print(f"  [SKIP]    '{tag}' already exists (id={existing_by_tag[tag]})")
                skipped += 1
                continue

        try:
            out = store.create(tag=tag, question=sc["question"], steps=sc["steps"])
            print(f"  [CREATE]  '{tag}' → id={out['id']} ({len(sc['steps'])} steps)")
            created += 1
        except Exception as e:
            print(f"  [ERROR]   '{tag}' — {e}")
            failed += 1

    final_count = store.count()
    print(
        f"\nDone. created={created} replaced={replaced} skipped={skipped} "
        f"failed={failed} | store size: {final_count}"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
