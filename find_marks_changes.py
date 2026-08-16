#!/usr/bin/env python3

import csv
import glob
import os
import re
from collections import defaultdict


ROOT = "./runs"


def parse_mark(value):
    """
    Convert:
        8/8       -> 8.0
        3.66667/8 -> 3.66667
        0/10      -> 0.0
    """
    if not value:
        return None

    value = value.strip()

    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)\s*$", value)
    if m:
        return float(m.group(1))

    return None


def find_summary_files(root):
    files = glob.glob(
        os.path.join(root, "**", "summary.csv"),
        recursive=True
    )

    # Sort by path. Since your directories use YYYY-MM-DD_HHMM,
    # this also gives chronological order.
    return sorted(files)


def get_run_name(path):
    """
    Example:
        lab_auto_grader/runs/lab_01/2026-08-07_1202/summary.csv

    returns:
        lab_01/2026-08-07_1202
    """
    parts = os.path.normpath(path).split(os.sep)

    # Find the "runs" directory
    try:
        i = parts.index("runs")
        return "/".join(parts[i + 1:-1])
    except ValueError:
        return path


def main():
    files = find_summary_files(ROOT)

    if not files:
        print(f"No summary.csv files found under: {ROOT}")
        return

    print(f"Found {len(files)} summary files:\n")

    # ---------------------------------------------------------
    # data[roll_no][question][run_name] = mark
    # ---------------------------------------------------------
    data = defaultdict(lambda: defaultdict(dict))

    question_names = set()

    for filepath in files:
        run_name = get_run_name(filepath)

        print(f"Reading: {filepath}")

        with open(filepath, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)

            if "roll_no" not in reader.fieldnames:
                print(f"  WARNING: no roll_no column: {filepath}")
                continue

            for row in reader:
                roll_no = row["roll_no"].strip()

                for column in reader.fieldnames:
                    if column in ("roll_no", "total", "anomalies"):
                        continue

                    # Remove markdown formatting if present,
                    # e.g. **q08_hailstone**
                    question = column.strip("*")

                    mark = parse_mark(row[column])

                    if mark is not None:
                        data[roll_no][question][run_name] = mark
                        question_names.add(question)

    question_names = sorted(question_names)

    # ---------------------------------------------------------
    # Print changes
    # ---------------------------------------------------------

    print("\n" + "=" * 100)
    print("MARK CHANGES")
    print("=" * 100)

    changes_found = False

    for roll_no in sorted(data):
        student_changes = []

        for question in question_names:
            runs = data[roll_no][question]

            # Only compare runs where the student has a mark
            ordered_runs = sorted(runs)

            if len(ordered_runs) < 2:
                continue

            changes = []

            previous_run = ordered_runs[0]
            previous_mark = runs[previous_run]

            for current_run in ordered_runs[1:]:
                current_mark = runs[current_run]

                if current_mark != previous_mark:
                    diff = current_mark - previous_mark

                    changes.append(
                        (
                            previous_run,
                            previous_mark,
                            current_run,
                            current_mark,
                            diff,
                        )
                    )

                previous_run = current_run
                previous_mark = current_mark

            if changes:
                student_changes.append((question, changes))

        if student_changes:
            changes_found = True

            print(f"\nRoll No: {roll_no}")
            print("-" * 100)

            for question, changes in student_changes:
                print(f"\n  {question}")

                for (
                    old_run,
                    old_mark,
                    new_run,
                    new_mark,
                    diff,
                ) in changes:
                    sign = "+" if diff > 0 else ""

                    print(
                        f"    {old_run}: {old_mark:g}"
                        f"  ->  "
                        f"{new_run}: {new_mark:g}"
                        f"  ({sign}{diff:g})"
                    )

    if not changes_found:
        print("\nNo mark changes found.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--directory", required=True, type=str,
        help="Directory containing the lab grade run directories"
    )

    args = parser.parse_args()

    ROOT = args.directory
    main()

# Example usage: python find_marks_changes.py --directory ./runs/lab_01
