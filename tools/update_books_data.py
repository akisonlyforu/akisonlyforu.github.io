"""
The purpose of this script is to update the /_data/books.yaml file that is used to build
the /library page of the website.
"""

import csv
import json
import pathlib
import re
import subprocess

from typing import Optional

#  I'll leave out children's authors, young-adult fiction, and bland fiction from what shows up on my website
IGNORED_AUTHORS = {
    "J.K. Rowling",  # Children's author
    "Garth Nix",  # Children's author
    "Lemony Snicket",  # Children's author
    "Jeffrey Archer",  # Meh
    "Matthew Reilly",  # Young Adult
    "Andy McNab",  # Meh
    "Dan Brown",  # Meh / Pop-Fiction
    "Neil Strauss",  # Eugh, bit gross
    "Christopher Paolini",  # Young Adult
    "Jeremy Clarkson",  # Blurgh
}

# For some reason Goodreads left out ISBN information for some of the books in my collection,
# even though the ISBN info is available in Goodreads if you look up the data on their website.
TITLE_TO_ISBN = {
    "China in Ten Words": "9780307739797",

}

# Goodreads didn't allow 1/2 stars in ratings annoyingly, but I want to try them.
RATING_OVERRIDES = {
    "Notes on Nationalism": "3.5",
    "The Return of the King (The Lord of the Rings, #3)": "4.5",
    "The New New Thing: A Silicon Valley Story": "3.5",
    "I Am a Strange Loop": "4.5",
    "Why Knowledge Matters: Rescuing Our Children from Failed Educational Theories": "4.5",
    "Flowers for Algernon": "4.5",
    "Neuromancer (Sprawl, #1)": "5",
    "Working in Public: The Making and Maintenance of Open Source Software": "3",
}


def fmt_rating(rating: str):
    if not rating:
        return "🤷‍♂️"
    rating = float(rating)
    if rating % 1 != 0:
        return ("★" * int(rating)) + "☆"
    return "★" * int(rating)


def get_book_review_path(book_reviews_directory: pathlib.Path, title: str, author: str) -> Optional[str]:
    cleaned_title = "-".join(re.sub("[!'.:]", "", title).split()).lower()
    cleaned_author = "-".join(re.sub("[!'.:]", "", author).split()).lower()
    expected_review_filename = f"{cleaned_title}-{cleaned_author}.md"
    expected_review_path = book_reviews_directory / expected_review_filename
    print(expected_review_path)
    if expected_review_path.exists():
        return f"{cleaned_title}-{cleaned_author}/"
    return None


if __name__ == "__main__":
    repo_root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8").strip()

    book_reviews_directory = pathlib.Path(repo_root, "collections", "_reviews")
    reading_data_csv_path = pathlib.Path(repo_root, "_data", "goodreads_library_export.csv")

    entries = []
    with open(reading_data_csv_path, newline="") as csvfile:
        spamreader = csv.DictReader(csvfile, delimiter=",", quotechar='"')
        for row in spamreader:
            entries.append(row)

    library_data = []
    antilibrary_data = []
    for row in entries:
        read_count = int(row["Read Count"])
        # TITLE_TO_ISBN takes precedence because some of the ISBNs in Goodreads data don't have cover available
        isbn = TITLE_TO_ISBN.get(row["Title"]) or row["ISBN"][2:-1] or row["ISBN13"][2:-1]
        review_path = get_book_review_path(
            book_reviews_directory=book_reviews_directory,
            title=row["Title"],
            author=row["Author"]
        )
        if review_path == "neal-stephenson":
            raise RuntimeError("what")
        entry = {
            "title": row["Title"],
            "author": row["Author"],
            "rating": fmt_rating(RATING_OVERRIDES[row["Title"]]) if RATING_OVERRIDES.get(row["Title"]) else fmt_rating(row["My Rating"]),
            "isbn": isbn,
            "review_path": review_path,
            "year_i_finished_reading": row["Date Read"].split("/")[0] if row["Date Read"] else None,
        }

        if not isbn:
            print(entry["title"] + " is missing its ISBN.")

        passes_filters = (
                entry["author"] not in IGNORED_AUTHORS
        )
        if not passes_filters:
            continue

        have_read = (
            # All books I've at least started will pass this filter
            read_count > 0 and
            # only books that I've finished reading show up on this shelf in Goodreads
            row["Exclusive Shelf"] == "read"
        )
        if have_read:
            library_data.append(entry)
        else:
            antilibrary_data.append(entry)

    library_data_output_path = pathlib.Path(repo_root, "_data", "library.yaml")
    with open(library_data_output_path, "w") as f:
        json.dump(library_data, f, indent=4)

    antilibrary_data_output_path = pathlib.Path(repo_root, "_data", "antilibrary.yaml")
    with open(antilibrary_data_output_path, "w") as f:
        json.dump(antilibrary_data, f, indent=4)
