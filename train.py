#!/usr/bin/env python3
"""Train a KMeans model from event.csv and export to JSON for Rust consumption."""

import argparse
import json
import sys

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler


def get_feature_columns(df):
    """Derive feature columns from the DataFrame (all columns except 'tid')."""
    return [col for col in df.columns if col != "tid"]


def find_best_k(X_scaled, k_range=range(2, 11)):
    """Use the elbow method (largest inertia drop) to pick K."""
    inertias = []
    for k in k_range:
        model = KMeans(n_clusters=k, n_init=10, random_state=42)
        model.fit(X_scaled)
        inertias.append(model.inertia_)
        print(f"  K={k}: inertia={model.inertia_:.2f}")

    # Find the K with the largest second derivative (elbow point)
    diffs = [inertias[i] - inertias[i + 1] for i in range(len(inertias) - 1)]
    diffs2 = [diffs[i] - diffs[i + 1] for i in range(len(diffs) - 1)]
    best_idx = np.argmax(diffs2) + 2  # +2 because diffs2 starts at k_range[2]
    best_k = list(k_range)[best_idx]
    return best_k, inertias


def train(csv_path, output_path, n_clusters=None):
    df = pd.read_csv(csv_path)
    print(f"Loaded {len(df)} tasks from {csv_path}")

    feature_columns = get_feature_columns(df)
    X = df[feature_columns].values
    tids = df["tid"].values

    # Standardize
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Determine K
    if n_clusters is None:
        print("Finding best K with elbow method...")
        n_clusters, _ = find_best_k(X_scaled)
        print(f"Selected K={n_clusters}")
    else:
        print(f"Using user-specified K={n_clusters}")

    # Train
    model = KMeans(n_clusters=n_clusters, n_init=10, random_state=42)
    model.fit(X_scaled)
    labels = model.labels_

    # Export model as JSON
    model_data = {
        "algorithm": "kmeans",
        "n_clusters": n_clusters,
        "features": feature_columns,
        "scaler": {
            "mean": scaler.mean_.tolist(),
            "std": scaler.scale_.tolist(),
        },
        "centroids": model.cluster_centers_.tolist(),
    }
    with open(output_path, "w") as f:
        json.dump(model_data, f, indent=2)
    print(f"Model saved to {output_path}")

    # Print cluster statistics
    print(f"\n{'='*60}")
    print(f"Classification results ({n_clusters} clusters)")
    print(f"{'='*60}")

    for c in range(n_clusters):
        mask = labels == c
        cluster_df = df[mask]
        print(f"\n--- Cluster {c} ({mask.sum()} tasks) ---")
        print(cluster_df[feature_columns].describe().to_string())

    # Build cluster membership with tgid from /proc
    print(f"\n{'='*60}")
    print("Cluster membership (tid, tgid, command)")
    print(f"{'='*60}")

    clusters = {}
    for c in range(n_clusters):
        mask = labels == c
        members = []
        for tid in tids[mask]:
            tgid = read_proc_field(tid, "Tgid")
            ppid = read_proc_field(tid, "PPid")
            comm = read_proc_comm(tid)
            members.append({
                "tid": int(tid),
                "tgid": tgid,
                "ppid": ppid,
                "command": comm,
            })
        clusters[f"cluster_{c}"] = members

    # Print
    for c in range(n_clusters):
        key = f"cluster_{c}"
        print(f"\n--- {key} ---")
        for m in clusters[key]:
            print(f"  tid={m['tid']}, tgid={m['tgid']}, ppid={m['ppid']}, cmd={m['command']}")

    # Save classification result
    result_path = output_path.replace(".json", "_result.json")
    with open(result_path, "w") as f:
        json.dump(clusters, f, indent=2)
    print(f"\nClassification result saved to {result_path}")


def read_proc_field(tid, field):
    """Read a field from /proc/<tid>/status."""
    try:
        with open(f"/proc/{tid}/status") as f:
            for line in f:
                if line.startswith(f"{field}:"):
                    return int(line.split(":")[1].strip())
    except (FileNotFoundError, ValueError, PermissionError):
        pass
    return None


def read_proc_comm(tid):
    """Read command name from /proc/<tid>/comm."""
    try:
        with open(f"/proc/{tid}/comm") as f:
            return f.read().strip()
    except (FileNotFoundError, PermissionError):
        return None


def main():
    parser = argparse.ArgumentParser(description="Train KMeans model from event.csv")
    parser.add_argument("csv", help="Path to event.csv")
    parser.add_argument("-o", "--output", default="model.json", help="Output model JSON path")
    parser.add_argument("-k", "--clusters", type=int, default=None,
                        help="Number of clusters (auto-detect if not specified)")
    parser.add_argument("--filter-tid", type=int, nargs="*", help="Filter by tid(s)")
    parser.add_argument("--filter-tgid", type=int, nargs="*", help="Filter by tgid(s)")
    parser.add_argument("--filter-cmd", nargs="*", help="Filter by command name(s)")
    args = parser.parse_args()

    df = pd.read_csv(args.csv)

    # Apply filters before training
    if args.filter_tid:
        df = df[df["tid"].isin(args.filter_tid)]
        print(f"Filtered to {len(df)} tasks by tid")
    if args.filter_tgid:
        tgid_set = set(args.filter_tgid)
        keep = [read_proc_field(tid, "Tgid") in tgid_set for tid in df["tid"]]
        df = df[keep]
        print(f"Filtered to {len(df)} tasks by tgid")
    if args.filter_cmd:
        cmd_set = set(args.filter_cmd)
        keep = [read_proc_comm(tid) in cmd_set for tid in df["tid"]]
        df = df[keep]
        print(f"Filtered to {len(df)} tasks by command")

    if len(df) == 0:
        print("No tasks remaining after filtering.", file=sys.stderr)
        sys.exit(1)

    # Write filtered CSV to a temp file and train from it
    filtered_path = args.csv + ".filtered.tmp"
    df.to_csv(filtered_path, index=False)
    train(filtered_path, args.output, args.clusters)

    import os
    os.remove(filtered_path)


if __name__ == "__main__":
    main()
