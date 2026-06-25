# YT Timestamp Generator — Backend

Flask backend for generating YouTube timestamps using Gemini AI.

## Environment Variables (set in Koyeb dashboard)

| Variable | Description |
|---|---|
| GEMINI_API_KEY | Your Gemini API key from aistudio.google.com |
| GOOGLE_SHEET_ID | Your Google Sheet ID (from the sheet URL) |
| GOOGLE_SHEET_NAME | Sheet tab name (default: Sheet1) |
| GOOGLE_SERVICE_ACCOUNT_JSON | Full contents of your service account .json file |

## Endpoints
- POST /api/timestamps
- POST /api/save-sheet
- GET  /api/batch-load
- POST /api/batch-save
- GET  /api/health
