-- Real banking backend. cogs/banking.py already calls database.deposit(),
-- database.withdraw(), database.transfer(), database.get_personal_account(),
-- database.create_personal_account(), database.get_cash_balance(), and
-- database.get_transaction_log_channel_id() -- none of those existed in
-- database.py before this. This file + the matching functions added to
-- database.py are that missing backend.
--
-- Cash (players.cash) and bank balance (bank_accounts.balance) are kept
-- separate, matching the original design doc: players carry cash, and a
-- bank account has to be manually opened by Bank Staff (!create-account)
-- before any of it can go into the bank. !dep moves cash -> bank balance,
-- !with moves bank balance -> cash, !transfer moves bank balance -> another
-- account's bank balance.
--
-- Run after schema.sql (players already exists).

ALTER TABLE players ADD COLUMN IF NOT EXISTS cash NUMERIC(14,2) NOT NULL DEFAULT 0;

CREATE SEQUENCE IF NOT EXISTS bank_account_seq START 1;

-- One account per player (matches banking.py's create_account / get_personal_account
-- calls, which assume a single account). state is the state it was opened in --
-- purely informational/for the transaction log, the account still works
-- account-number-to-account-number across states (see !transfer).
CREATE TABLE IF NOT EXISTS bank_accounts (
    account_number  TEXT PRIMARY KEY,
    discord_id      BIGINT NOT NULL UNIQUE REFERENCES players(discord_id),
    state           TEXT NOT NULL,
    balance         NUMERIC(14,2) NOT NULL DEFAULT 0,
    created_by      BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
