from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import os
from collections import defaultdict

app = Flask(__name__)
CORS(app)

DB_PATH = "expense_splitter.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            description TEXT NOT NULL,
            amount REAL NOT NULL,
            paid_by INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
            FOREIGN KEY (paid_by) REFERENCES members(id)
        );

        CREATE TABLE IF NOT EXISTS expense_splits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            expense_id INTEGER NOT NULL,
            member_id INTEGER NOT NULL,
            share REAL NOT NULL,
            FOREIGN KEY (expense_id) REFERENCES expenses(id) ON DELETE CASCADE,
            FOREIGN KEY (member_id) REFERENCES members(id)
        );
    """)
    conn.commit()
    conn.close()

def calculate_settlements(group_id):
    conn = get_db()
    cursor = conn.cursor()

    # Get all members
    cursor.execute("SELECT id, name FROM members WHERE group_id = ?", (group_id,))
    members = {row["id"]: row["name"] for row in cursor.fetchall()}

    # Calculate net balance for each member
    balances = defaultdict(float)

    cursor.execute("""
        SELECT e.paid_by, es.member_id, es.share
        FROM expenses e
        JOIN expense_splits es ON e.id = es.expense_id
        WHERE e.group_id = ?
    """, (group_id,))

    for row in cursor.fetchall():
        payer = row["paid_by"]
        member = row["member_id"]
        share = row["share"]
        # Payer gets credit (positive)
        balances[payer] += share
        # Member owes (negative) unless they're the payer
        balances[member] -= share

    conn.close()

    # Simplify debts using greedy algorithm
    settlements = []
    creditors = [(id, bal) for id, bal in balances.items() if bal > 0.01]
    debtors = [(id, -bal) for id, bal in balances.items() if bal < -0.01]

    creditors.sort(key=lambda x: x[1], reverse=True)
    debtors.sort(key=lambda x: x[1], reverse=True)

    i, j = 0, 0
    while i < len(creditors) and j < len(debtors):
        cred_id, cred_amt = creditors[i]
        debt_id, debt_amt = debtors[j]

        amount = min(cred_amt, debt_amt)
        if amount > 0.01:
            settlements.append({
                "from": members.get(debt_id, "Unknown"),
                "from_id": debt_id,
                "to": members.get(cred_id, "Unknown"),
                "to_id": cred_id,
                "amount": round(amount, 2)
            })

        creditors[i] = (cred_id, cred_amt - amount)
        debtors[j] = (debt_id, debt_amt - amount)

        if creditors[i][1] < 0.01:
            i += 1
        if debtors[j][1] < 0.01:
            j += 1

    return settlements

# ─── GROUP ROUTES ──────────────────────────────────────────────────────────────

@app.route("/api/groups", methods=["GET"])
def get_groups():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT g.id, g.name, g.created_at,
               COUNT(DISTINCT m.id) as member_count,
               COUNT(DISTINCT e.id) as expense_count,
               COALESCE(SUM(e.amount), 0) as total_amount
        FROM groups g
        LEFT JOIN members m ON g.id = m.group_id
        LEFT JOIN expenses e ON g.id = e.group_id
        GROUP BY g.id
        ORDER BY g.created_at DESC
    """)
    groups = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(groups)

@app.route("/api/groups", methods=["POST"])
def create_group():
    data = request.json
    name = data.get("name", "").strip()
    members = data.get("members", [])

    if not name:
        return jsonify({"error": "Group name is required"}), 400
    if len(members) < 2:
        return jsonify({"error": "At least 2 members required"}), 400

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO groups (name) VALUES (?)", (name,))
    group_id = cursor.lastrowid

    member_ids = []
    for m in members:
        m = m.strip()
        if m:
            cursor.execute("INSERT INTO members (group_id, name) VALUES (?, ?)", (group_id, m))
            member_ids.append({"id": cursor.lastrowid, "name": m})

    conn.commit()
    conn.close()
    return jsonify({"id": group_id, "name": name, "members": member_ids}), 201

@app.route("/api/groups/<int:group_id>", methods=["GET"])
def get_group(group_id):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM groups WHERE id = ?", (group_id,))
    group = cursor.fetchone()
    if not group:
        return jsonify({"error": "Group not found"}), 404

    cursor.execute("SELECT * FROM members WHERE group_id = ?", (group_id,))
    members = [dict(row) for row in cursor.fetchall()]

    cursor.execute("""
        SELECT e.*, m.name as paid_by_name
        FROM expenses e
        JOIN members m ON e.paid_by = m.id
        WHERE e.group_id = ?
        ORDER BY e.created_at DESC
    """, (group_id,))
    expenses = []
    for exp in cursor.fetchall():
        exp_dict = dict(exp)
        cursor.execute("""
            SELECT es.share, m.name, m.id
            FROM expense_splits es
            JOIN members m ON es.member_id = m.id
            WHERE es.expense_id = ?
        """, (exp_dict["id"],))
        exp_dict["splits"] = [dict(r) for r in cursor.fetchall()]
        expenses.append(exp_dict)

    settlements = calculate_settlements(group_id)
    conn.close()

    return jsonify({
        "group": dict(group),
        "members": members,
        "expenses": expenses,
        "settlements": settlements
    })

@app.route("/api/groups/<int:group_id>", methods=["DELETE"])
def delete_group(group_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM groups WHERE id = ?", (group_id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "Group deleted"})

# ─── EXPENSE ROUTES ────────────────────────────────────────────────────────────

@app.route("/api/groups/<int:group_id>/expenses", methods=["POST"])
def add_expense(group_id):
    data = request.json
    description = data.get("description", "").strip()
    amount = data.get("amount")
    paid_by = data.get("paid_by")
    split_type = data.get("split_type", "equal")  # equal | custom
    splits = data.get("splits", {})  # {member_id: amount}

    if not description:
        return jsonify({"error": "Description required"}), 400
    if not amount or float(amount) <= 0:
        return jsonify({"error": "Valid amount required"}), 400
    if not paid_by:
        return jsonify({"error": "Payer required"}), 400

    conn = get_db()
    cursor = conn.cursor()

    # Verify group and members exist
    cursor.execute("SELECT id FROM members WHERE group_id = ?", (group_id,))
    members = [row["id"] for row in cursor.fetchall()]
    if not members:
        return jsonify({"error": "Group not found"}), 404

    cursor.execute(
        "INSERT INTO expenses (group_id, description, amount, paid_by) VALUES (?, ?, ?, ?)",
        (group_id, description, float(amount), paid_by)
    )
    expense_id = cursor.lastrowid

    if split_type == "equal":
        share = round(float(amount) / len(members), 2)
        for mid in members:
            cursor.execute(
                "INSERT INTO expense_splits (expense_id, member_id, share) VALUES (?, ?, ?)",
                (expense_id, mid, share)
            )
    else:
        # Custom splits
        for mid_str, share in splits.items():
            cursor.execute(
                "INSERT INTO expense_splits (expense_id, member_id, share) VALUES (?, ?, ?)",
                (expense_id, int(mid_str), float(share))
            )

    conn.commit()
    conn.close()
    return jsonify({"id": expense_id, "message": "Expense added"}), 201

@app.route("/api/expenses/<int:expense_id>", methods=["DELETE"])
def delete_expense(expense_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "Expense deleted"})

@app.route("/api/groups/<int:group_id>/summary", methods=["GET"])
def get_summary(group_id):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT m.name, COALESCE(SUM(e.amount), 0) as total_paid
        FROM members m
        LEFT JOIN expenses e ON m.id = e.paid_by AND e.group_id = ?
        WHERE m.group_id = ?
        GROUP BY m.id
    """, (group_id, group_id))
    paid_summary = [dict(row) for row in cursor.fetchall()]

    settlements = calculate_settlements(group_id)
    conn.close()

    return jsonify({
        "paid_summary": paid_summary,
        "settlements": settlements
    })

if __name__ == "__main__":
    init_db()
    print("🚀 Expense Splitter API running on http://localhost:5000")
    app.run(debug=True, port=5000)