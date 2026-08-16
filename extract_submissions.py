"""
# Example usage:
python extract_submissions.py \
    --archive-dir /path/to/archives \
    --lab-dir /path/to/lab \
    --student-csv /path/to/students.csv \
    --out-csv /path/to/ip_student_mapping.csv
"""

import argparse
import csv
import shutil
import tarfile
import tempfile
from pathlib import Path


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Extract student submissions and create IP-to-student mapping."
    )

    parser.add_argument(
        "--archive-dir",
        type=Path,
        required=True,
        help="Directory containing .tar.gz submission archives",
    )

    parser.add_argument(
        "--lab-dir",
        type=Path,
        required=True,
        help="Directory where student folders will be placed",
    )
    
    parser.add_argument(
        "--student-csv",
        type=Path,
        required=True,
        help="CSV containing Name and Roll Number columns",
    )

    parser.add_argument(
        "--out-csv",
        type=Path,
        default=None,
        help="Output CSV file. Defaults to <lab-dir>/ip_student_mapping.csv",
    )

    return parser.parse_args()


def remove_non_c_files(student_dir):
    """Remove all files in the student directory that are not .c files."""
    for path in student_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() != ".c":
            print(f"    Removing: {path}")
            path.unlink()
            

def main():
    args = parse_args()

    archive_dir = args.archive_dir
    lab_dir = args.lab_dir
    csv_file = args.out_csv or (lab_dir / "ip_student_mapping.csv")
    
    student_csv = args.student_csv

    if not student_csv.is_file():
        print(f"ERROR: Student CSV does not exist: {student_csv}")
        return

    # Load Roll Number -> Name mapping
    student_mapping = {}

    with student_csv.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            roll_number = row["roll_no"].strip()
            name = row["name"].strip()

            student_mapping[roll_number] = name

    # Create lab directory if it doesn't exist
    lab_dir.mkdir(parents=True, exist_ok=True)

    # Check archive directory
    if not archive_dir.is_dir():
        print(f"ERROR: Archive directory does not exist: {archive_dir}")
        return

    archives = sorted(archive_dir.glob("*.tar.gz"))

    if not archives:
        print(f"No .tar.gz files found in {archive_dir}")
        return

    mapping = []

    print(f"Archive directory : {archive_dir}")
    print(f"Lab directory     : {lab_dir}")
    print(f"CSV file          : {csv_file}")
    print(f"Found {len(archives)} archives.\n")

    for archive in archives:
        # Example:
        # 10.129.5.10.tar.gz -> 10.129.5.10
        ip = archive.name.removesuffix(".tar.gz")

        print(f"Processing: {archive.name}")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Extract archive
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(temp_path)

            # Find directories inside extracted archive
            directories = [
                p for p in temp_path.iterdir()
                if p.is_dir()
            ]

            if not directories:
                print(f"  WARNING: No directory found in {archive.name}")
                continue

            if len(directories) > 1:
                print(
                    f"  WARNING: Multiple directories found: "
                    f"{[d.name for d in directories]}"
                )

            # Expected:
            # extracted/
            # └── 142401023/
            student_dir = directories[0]
            student_id = student_dir.name

            destination = lab_dir / student_id

            # Don't overwrite an existing submission
            if destination.exists():
                print(
                    f"  WARNING: {destination} already exists. "
                    f"Skipping."
                )
                continue

            # Move student folder
            shutil.move(str(student_dir), str(destination))
            
            # Remove everything except .c files
            remove_non_c_files(destination)

            name = student_mapping.get(student_id)

            if name is None:
                print(
                    f"  WARNING: No name found for student ID {student_id}"
                )
                name = ""

            mapping.append({
                "ip": ip,
                "roll_no": student_id,
                "name": name,
            })

            print(f"  {ip} -> {student_id}")

    # Write CSV
    with csv_file.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["ip", "roll_no", "name"]
        )

        writer.writeheader()
        writer.writerows(mapping)

    print("\nDone!")
    print(f"Student folders : {lab_dir}")
    print(f"Archives         : {archive_dir}")
    print(f"Student CSV      : {student_csv}")
    print(f"Mapping CSV     : {csv_file}")
    print(f"Processed       : {len(mapping)} archives")


if __name__ == "__main__":
    main()
