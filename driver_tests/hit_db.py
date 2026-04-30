import sqlite3
con = sqlite3.connect(r"C:\SportsReplay\metadata.db")

# print number of rows
print(con.execute("SELECT count(*) from segments").fetchone()[0])


for row in con.execute("SELECT segment_id, fragment_index, file_path, start_pts_ns, end_pts_ns, frame_count_estimate, size_bytes, state FROM segments ORDER BY segment_id"):
    print(row)


print()
print("plays row count:")
try:
    print(con.execute("SELECT count(*) FROM plays").fetchone()[0])
    print("plays rows:")
    for row in con.execute("SELECT * FROM plays ORDER BY rowid"):
        print(row)
except sqlite3.OperationalError as exc:
    print(f"Could not query plays table: {exc}")
