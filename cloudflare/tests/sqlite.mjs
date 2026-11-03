import {DatabaseSync} from "node:sqlite";

// Tests execute the production StoreEngine against SQLite, not a map replica.
export function sqliteStorage(path = ":memory:") {
  const db = new DatabaseSync(path);
  let counter = 0;
  return {
    db,
    sql: {exec(sql, ...args) {
      if (!args.length && sql.includes(";")) { db.exec(sql); return []; }
      return db.prepare(sql).all(...args);
    }},
    transactionSync(fn) {
      const name = `test_${++counter}`;
      db.exec(`SAVEPOINT ${name}`);
      try { const value = fn(); db.exec(`RELEASE ${name}`); return value; }
      catch (error) { db.exec(`ROLLBACK TO ${name}; RELEASE ${name}`); throw error; }
    },
  };
}
