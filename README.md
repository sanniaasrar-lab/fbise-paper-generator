# ScholarPaper Generator

ScholarPaper Generator creates FBISE Grade 9 or 10 Biology, Physics, Chemistry, or Computer Science exams with 1–60 MCQs, 2–12 short questions, and 2–4 long questions. Short- and long-question counts must be even so each A-or-B pair stays complete. Papers are saved in a local SQLite database and can be browsed or downloaded as PDFs. Generated questions are also shown in searchable tables and can be copied or downloaded as JSON. MCQs are worth 0.5 mark each; each short pair adds 3 attempted marks and each long pair adds 6 attempted marks.

Choose whether to generate a question-only paper or include MCQ explanations and written model answers. The selected mode applies to the displayed paper, print view, and JSON export.

## Run locally

1. Create and activate a virtual environment.
2. Install dependencies with `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` and set `OPENAI_API_KEY` to a newly issued key.
4. Start the production WSGI server with `.venv\Scripts\waitress-serve.exe --listen=127.0.0.1:5057 main:app`.
5. Open `http://127.0.0.1:5057` in your browser. The server binds to this computer only; configure a trusted reverse proxy before making it publicly accessible.

The paper's attempted total varies with the selected question counts: 0.5 mark per MCQ, plus 3 marks per short-question pair and 6 marks per long-question pair. Generated content is practice material and should be reviewed against the current syllabus.

If an API key was pasted into a chat, source file, or other shared location, revoke it in the OpenAI dashboard and create a replacement before using the app.

## Deploy for free on Render

1. Create a GitHub repository and upload this project.
2. Sign in to Render and select **New > Blueprint**.
3. Choose this repository and approve `render.yaml`.
4. Render provides a public HTTPS address such as `https://your-app.onrender.com` after deployment.

The app uses offline question generation by default, so no API key or environment variable is needed. The Blueprint uses Waitress as the production server. Keep `.env`, `.venv`, and any real credentials out of Git.