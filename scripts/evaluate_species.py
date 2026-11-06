"""Offline evaluation of SpeciesNet on a labelled image set.

Usage:
    python scripts/evaluate_species.py --images <dir> --labels labels.csv

labels.csv columns:
    filename,true_label,condition (optional)
    - filename: image file name under --images
    - true_label: expected species common name (SpeciesNet vocabulary) or 'empty'
    - condition: optional tag (night, blur, empty, partial, ...)

Writes results to eval_results/<timestamp>/.
"""
import argparse
import csv
import json
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from app.ai import infer


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", required=True)
    ap.add_argument("--labels", required=True)
    ap.add_argument("--out", default="eval_results")
    args = ap.parse_args()

    img_dir = Path(args.images)
    out_root = Path(args.out)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = out_root / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    with open(args.labels, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(r)

    results = []
    for r in rows:
        path = img_dir / r["filename"]
        data = path.read_bytes()
        try:
            res = infer(data)
            err = None
        except Exception as e:
            res = {"state": "error", "species": None, "confidence": None}
            err = f"{type(e).__name__}: {e}"
        results.append({
            "filename": r["filename"],
            "true_label": r.get("true_label", ""),
            "condition": r.get("condition", ""),
            "predicted_species": res.get("species"),
            "confidence": res.get("confidence"),
            "state": res.get("state"),
            "error": err,
        })

    with open(out_dir / "results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)

    # metrics
    total = len(results)
    correct = sum(1 for r in results if r["predicted_species"] == r["true_label"] and r["state"] == "completed")
    accuracy = correct / total if total else 0.0
    unknown = sum(1 for r in results if r["state"] == "unknown")
    unavailable = sum(1 for r in results if r["state"] == "unavailable")

    classes = sorted({r["true_label"] for r in results})
    # confusion matrix
    cm = {a: {b: 0 for b in classes} for a in classes}
    for r in results:
        t = r["true_label"]
        p = r["predicted_species"] if r["predicted_species"] is not None else "__none__"
        if p not in cm[t]:
            cm[t][p] = 0
            if p not in classes:
                classes = classes + [p]
        cm[t][p] += 1
    # rewrite cm with full class list
    allcls = sorted(set(classes))
    cm = {a: {b: 0 for b in allcls} for a in allcls}
    for r in results:
        t = r["true_label"]
        p = r["predicted_species"] if r["predicted_species"] is not None else "__none__"
        if t not in cm:
            cm[t] = {b: 0 for b in allcls}
        if p not in cm[t]:
            for a in cm:
                cm[a][p] = cm[a].get(p, 0)
            allcls.append(p)
        cm[t][p] += 1

    # write confusion csv
    with open(out_dir / "confusion_matrix.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["true\\pred"] + allcls)
        for a in allcls:
            w.writerow([a] + [cm.get(a, {}).get(b, 0) for b in allcls])

    # plot
    fig, ax = plt.subplots(figsize=(8, 6))
    data = [[cm.get(a, {}).get(b, 0) for b in allcls] for a in allcls]
    ax.imshow(data, cmap="Blues")
    ax.set_xticks(range(len(allcls)))
    ax.set_yticks(range(len(allcls)))
    ax.set_xticklabels(allcls, rotation=45, ha="right")
    ax.set_yticklabels(allcls)
    ax.set_xlabel("predicted")
    ax.set_ylabel("true")
    for i in range(len(allcls)):
        for j in range(len(allcls)):
            ax.text(j, i, data[i][j], ha="center", va="center")
    fig.tight_layout()
    fig.savefig(out_dir / "confusion_matrix.png")
    plt.close(fig)

    # per-class precision/recall
    per_class = {}
    for c in allcls:
        tp = cm.get(c, {}).get(c, 0)
        fp = sum(cm.get(t, {}).get(c, 0) for t in allcls if t != c)
        fn = sum(cm.get(c, {}).get(p, 0) for p in allcls if p != c)
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        per_class[c] = {"precision": prec, "recall": rec, "support": sum(cm.get(c, {}).values())}

    # conflict species false-alarm rate: treat 'empty' true labels; prediction of any species on empty = false alarm
    conflict_species = None
    empty_total = sum(1 for r in results if r["true_label"].lower() == "empty")
    false_alarms = sum(1 for r in results if r["true_label"].lower() == "empty" and r["predicted_species"] is not None and r["state"] == "completed")
    far = false_alarms / empty_total if empty_total else 0.0

    # threshold sweep (conflict species treated as all non-empty species)
    thresholds = [round(0.3 + 0.1 * i, 1) for i in range(7)]
    sweep = []
    for t in thresholds:
        tp = fp = fn = tn = 0
        for r in results:
            true_empty = r["true_label"].lower() == "empty"
            pred = r["predicted_species"]
            conf = r["confidence"] or 0.0
            predicted_conflict = (pred is not None and not true_empty and conf >= t)
            if predicted_conflict and not true_empty:
                tp += 1
            elif predicted_conflict and true_empty:
                fp += 1
            elif (not predicted_conflict) and not true_empty:
                fn += 1
            else:
                tn += 1
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        far_t = fp / (fp + tn) if (fp + tn) else 0.0
        sweep.append({"threshold": t, "precision": prec, "recall": rec, "false_alarm_rate": far_t})

    with open(out_dir / "threshold_sweep.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["threshold", "precision", "recall", "false_alarm_rate"])
        w.writeheader()
        w.writerows(sweep)

    metrics = {
        "total": total,
        "accuracy": accuracy,
        "unknown_count": unknown,
        "unavailable_count": unavailable,
        "false_alarm_rate_empty": far,
        "per_class": per_class,
    }
    with open(out_dir / "metrics.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)

    # accuracy by condition
    if any(r.get("condition") for r in rows):
        by_cond = {}
        for r in results:
            c = r["condition"] or "unspecified"
            by_cond.setdefault(c, {"total": 0, "correct": 0})
            by_cond[c]["total"] += 1
            if r["predicted_species"] == r["true_label"] and r["state"] == "completed":
                by_cond[c]["correct"] += 1
        for c in by_cond:
            t = by_cond[c]["total"]
            by_cond[c]["accuracy"] = by_cond[c]["correct"] / t if t else 0.0
        with open(out_dir / "accuracy_by_condition.json", "w", encoding="utf-8") as f:
            json.dump(by_cond, f, indent=2)

    print(f"Wrote results to {out_dir}")


if __name__ == "__main__":
    main()
"""