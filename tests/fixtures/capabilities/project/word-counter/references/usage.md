# word-counter usage

Input (JSON on standard input): `{"text": "any text up to 10000 characters"}`

Output (one JSON document on standard output): `{"words": <number>}`

Words are runs of characters separated by whitespace. Diagnostics go to
standard error; a non-zero exit status means the count could not be produced.
