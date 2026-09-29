# Sample batches

Demo input for the dashboard's "Run a batch" tab (same 20 questions in both
formats). Every name, SSN, card, account, routing number, IBAN, email, and
phone number here is **synthetic** (test card numbers, `example.com` emails,
555 phone numbers, the IBAN standard's example IBAN). Half the questions carry
no PII; two are out of scope and should be refused.

- `policy_questions.csv`: needs a `question` column; other columns are ignored.
- `policy_questions.jsonl`: one `{"question": ...}` object per line.
