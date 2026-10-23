import io
import json
import os
import re
import sqlite3
from contextlib import closing
from xml.sax.saxutils import escape

from flask import Flask, abort, render_template, request, send_file, url_for
from openai import OpenAI
from dotenv import load_dotenv
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from subject_bank import build_offline_exam

load_dotenv()
app = Flask(__name__, template_folder="Templates")

MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-nano")
SUBJECTS = ("Biology", "Physics", "Chemistry", "Computer Science")
GRADES = ("9", "10")
DIFFICULTIES = ("Easy", "Medium", "Hard")
MAX_MCQS = 60
MIN_SHORT_QUESTIONS = 2
MAX_SHORT_QUESTIONS = 12
MIN_LONG_QUESTIONS = 2
MAX_LONG_QUESTIONS = 4
DATABASE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "papers.db")


def init_db() -> None:
	with closing(sqlite3.connect(DATABASE_PATH)) as conn:
		with conn:
			conn.execute(
				"""
				CREATE TABLE IF NOT EXISTS saved_papers (
					id INTEGER PRIMARY KEY AUTOINCREMENT,
					title TEXT NOT NULL,
					subject TEXT NOT NULL,
					grade TEXT NOT NULL,
					difficulty TEXT NOT NULL,
					solutions_included BOOLEAN NOT NULL,
					json_data TEXT NOT NULL,
					created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
				)
				"""
			)


def save_paper_to_db(exam: dict) -> int:
	with closing(sqlite3.connect(DATABASE_PATH)) as conn:
		with conn:
			cursor = conn.execute(
				"""
				INSERT INTO saved_papers
					(title, subject, grade, difficulty, solutions_included, json_data)
				VALUES (?, ?, ?, ?, ?, ?)
				""",
				(
					exam["title"],
					exam["subject"],
					exam["grade"],
					exam.get("difficulty", "Medium"),
					exam["solutions_included"],
					json.dumps(exam),
				),
			)
			paper_id = cursor.lastrowid
	return paper_id


init_db()


def build_exam(topic: str = "", include_solutions: bool = False, subject: str = "Computer Science", grade: str = "9", difficulty: str = "Medium", num_mcqs_target: int = MAX_MCQS, num_short_target: int = MAX_SHORT_QUESTIONS, num_long_target: int = MAX_LONG_QUESTIONS) -> dict:
	if subject not in SUBJECTS:
		raise ValueError("Choose Biology, Physics, Chemistry, or Computer Science.")
	if grade not in GRADES:
		raise ValueError("Choose Class 9 or Class 10.")
	if difficulty not in DIFFICULTIES:
		raise ValueError("Choose Easy, Medium, or Hard difficulty.")
	if not isinstance(num_mcqs_target, int) or not 1 <= num_mcqs_target <= MAX_MCQS:
		raise ValueError(f"Number of MCQs must be between 1 and {MAX_MCQS}.")
	validate_written_counts(num_short_target, num_long_target)

	client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
	topic_instruction = f"Give extra emphasis to this topic: {topic}." if topic else "Cover the syllabus broadly and evenly."
	difficulty_instruction = f"Set the difficulty level to {difficulty} (use appropriate complexity for Grade {grade} students)."
	mcq_fields = (
		', "answer" (one letter A-D), and "solution" (a concise explanation of why that answer is correct)'
		if include_solutions else
		''
	)
	def request_json(prompt: str, max_output_tokens: int = 5000) -> dict:
		response = client.responses.create(
			model=MODEL,
			input=prompt,
			text={"format": {"type": "json_object"}},
			max_output_tokens=max_output_tokens,
		)
		if not response.output_text:
			raise ValueError("OpenAI returned an empty response. Please try again.")
		return json.loads(response.output_text)

	mcqs = []
	batch_size = 20
	for first_number in range(1, num_mcqs_target + 1, batch_size):
		current_batch_size = min(batch_size, num_mcqs_target - first_number + 1)
		last_number = first_number + current_batch_size - 1
		batch = request_json(f"""
Create exactly {current_batch_size} original Grade {grade} {subject} practice MCQs for the FBISE 2025-26 syllabus.
{difficulty_instruction}
Use concepts and terminology appropriate to {subject}; keep every question within the Grade {grade} syllabus.
{topic_instruction}
Return a JSON object with one key, "mcqs", whose value is an array of exactly {current_batch_size} objects.
Number them from {first_number} through {last_number} in order. Each object must have exactly these fields: "number", "question", "options" (exactly A, B, C, D), and "topic"{mcq_fields}.
Use concise questions, age-appropriate language, plausible distractors, and one correct answer.
Do not include an introduction or any other keys.
""", max_output_tokens=6000)
		batch_mcqs = batch.get("mcqs", [])
		if len(batch_mcqs) != current_batch_size:
			raise ValueError(f"OpenAI returned {len(batch_mcqs)} MCQs for batch {first_number}-{last_number}; expected {current_batch_size}. Please generate again.")
		mcqs.extend(batch_mcqs)

	written_fields = (
		' and "solution" with a concise, accurate model answer appropriate to the marks'
		if include_solutions else
		''
	)
	written = request_json(f"""
Create practice written questions for Grade {grade} {subject} based on the FBISE 2025-26 syllabus.
{difficulty_instruction}
Use concepts and terminology appropriate to {subject}; keep every question within the Grade {grade} syllabus.
{topic_instruction}
Return only a JSON object with two arrays:
- "short_questions": exactly {num_short_target} distinct, concise questions suitable for 3 marks each.
- "long_questions": exactly {num_long_target} distinct, multi-part questions suitable for 6 marks each.
Every item must have only "number", "question", and "topic" fields{written_fields}. Number each array from 1.
Do not encode OR groups or A/B choices; the application adds those deterministically.
Avoid duplicates, use age-appropriate language, and do not claim textbook wording verbatim.
""", max_output_tokens=5000)
	short_questions = written.get("short_questions", [])
	long_questions = written.get("long_questions", [])
	if len(short_questions) != num_short_target or len(long_questions) != num_long_target:
		raise ValueError("OpenAI returned incomplete written questions. Please generate again.")
	for number, question in enumerate(short_questions, start=1):
		question.update(number=number, group=(number + 1) // 2, choice="A" if number % 2 else "B", marks=3)
	for number, question in enumerate(long_questions, start=1):
		question.update(number=number, group=(number + 1) // 2, choice="A" if number % 2 else "B", marks=6)

	exam = {
		"title": f"FBISE Grade {grade} {subject} Exams ({difficulty})",
		"subject": subject,
		"grade": grade,
		"difficulty": difficulty,
		"total_marks": calculate_total_marks(len(mcqs), len(short_questions), len(long_questions)),
		"solutions_included": include_solutions,
		"source": "AI",
		"mcqs": mcqs,
		"short_questions": short_questions,
		"long_questions": long_questions,
	}
	for question in exam["mcqs"]:
		normalize_mcq(question)
		if not include_solutions:
			question.pop("answer", None)
			question.pop("solution", None)
	if not include_solutions:
		for question in exam["short_questions"] + exam["long_questions"]:
			question.pop("answer", None)
			question.pop("solution", None)
	validate_exam(exam, include_solutions, num_mcqs_target, num_short_target, num_long_target)
	return exam


def normalize_mcq(question: dict) -> None:
	options = question.get("options")
	if isinstance(options, list) and len(options) == 4:
		options = dict(zip(("A", "B", "C", "D"), options))
	if not isinstance(options, dict):
		return

	normalized_options = {}
	for label, value in options.items():
		match = re.match(r"\s*(?:option\s*)?([A-D])(?:\b|[).:\-])", str(label), re.IGNORECASE)
		if match:
			if isinstance(value, dict):
				value = value.get("text", value.get("value", ""))
			normalized_options[match.group(1).upper()] = str(value).strip()
	question["options"] = normalized_options

	answer = str(question.get("answer", "")).strip()
	answer_match = re.match(r"\s*(?:option\s*)?([A-D])(?:\b|[).:\-])", answer, re.IGNORECASE)
	if answer_match:
		question["answer"] = answer_match.group(1).upper()
	else:
		for label, option_text in normalized_options.items():
			if answer.casefold() == option_text.casefold():
				question["answer"] = label
				break


def validate_written_counts(num_short: int, num_long: int) -> None:
	if not isinstance(num_short, int) or not MIN_SHORT_QUESTIONS <= num_short <= MAX_SHORT_QUESTIONS or num_short % 2:
		raise ValueError(f"Short-question count must be an even number from {MIN_SHORT_QUESTIONS} to {MAX_SHORT_QUESTIONS}.")
	if not isinstance(num_long, int) or not MIN_LONG_QUESTIONS <= num_long <= MAX_LONG_QUESTIONS or num_long % 2:
		raise ValueError(f"Long-question count must be an even number from {MIN_LONG_QUESTIONS} to {MAX_LONG_QUESTIONS}.")


def calculate_total_marks(num_mcqs: int, num_short: int, num_long: int) -> float:
	return num_mcqs * 0.5 + (num_short // 2) * 3 + (num_long // 2) * 6


def validate_exam(exam: dict, include_solutions: bool = False, expected_mcq_count: int = MAX_MCQS, expected_short_count: int = MAX_SHORT_QUESTIONS, expected_long_count: int = MAX_LONG_QUESTIONS) -> None:
	required = ("title", "subject", "grade", "total_marks", "solutions_included", "mcqs", "short_questions", "long_questions")
	if not isinstance(exam, dict) or any(key not in exam for key in required):
		raise ValueError("The generated exam is missing required sections. Please generate it again.")
	if exam["solutions_included"] is not include_solutions:
		raise ValueError("The generated paper does not match the selected solution mode. Please generate it again.")
	if not isinstance(expected_mcq_count, int) or not 1 <= expected_mcq_count <= MAX_MCQS:
		raise ValueError(f"Number of MCQs must be between 1 and {MAX_MCQS}.")
	if len(exam["mcqs"]) != expected_mcq_count:
		raise ValueError(f"The response did not contain exactly {expected_mcq_count} MCQs. Please generate it again.")
	validate_written_counts(expected_short_count, expected_long_count)
	if len(exam["short_questions"]) != expected_short_count or len(exam["long_questions"]) != expected_long_count:
		raise ValueError("The short- or long-question count was incomplete. Please generate it again.")
	for questions, expected_groups, expected_marks in (
		(exam["short_questions"], expected_short_count // 2, 3),
		(exam["long_questions"], expected_long_count // 2, 6),
	):
		groups = {}
		for question in questions:
			if question.get("marks") != expected_marks or question.get("choice") not in {"A", "B"}:
				raise ValueError("A written question has invalid choice or marks data. Please generate it again.")
			groups.setdefault(question.get("group"), []).append(question["choice"])
		if len(groups) != expected_groups or any(sorted(choices) != ["A", "B"] for choices in groups.values()):
			raise ValueError("Written questions must be arranged in complete A OR B pairs. Please generate it again.")
	expected_total_marks = calculate_total_marks(expected_mcq_count, expected_short_count, expected_long_count)
	if exam["total_marks"] != expected_total_marks:
		raise ValueError(f"The exam total must be {expected_total_marks:g} attempted marks. Please generate it again.")
	for question in exam["mcqs"]:
		if not isinstance(question.get("options"), dict) or set(question["options"]) != {"A", "B", "C", "D"}:
			raise ValueError("An MCQ is missing one or more answer options. Please generate it again.")
		if include_solutions:
			if question.get("answer") not in {"A", "B", "C", "D"} or not str(question.get("solution", "")).strip():
				raise ValueError("An MCQ is missing its answer or solution. Please generate it again.")
		elif "answer" in question or "solution" in question:
			raise ValueError("A question-only paper must not include answer or solution data. Please generate it again.")
	for question in exam["short_questions"] + exam["long_questions"]:
		if include_solutions:
			if not str(question.get("solution", "")).strip():
				raise ValueError("A written question is missing its solution. Please generate it again.")
		elif "answer" in question or "solution" in question:
			raise ValueError("A question-only paper must not include answer or solution data. Please generate it again.")


@app.route("/", methods=["GET", "POST"])
def home():
	exam = None
	error = None
	topic = ""
	mode = "questions"
	subject = "Computer Science"
	grade = "9"
	difficulty = "Medium"
	generator = "offline"
	num_mcqs = MAX_MCQS
	num_short = MAX_SHORT_QUESTIONS
	num_long = MAX_LONG_QUESTIONS
	invalid_mcq_count = False
	invalid_written_counts = False
	if request.method == "POST":
		topic = request.form.get("topic", "").strip()[:120]
		mode = request.form.get("mode", "questions")
		subject = request.form.get("subject", "Computer Science")
		grade = request.form.get("grade", "9")
		difficulty = request.form.get("difficulty", "Medium")
		generator = request.form.get("generator", "offline")
		try:
			num_mcqs = int(request.form.get("num_mcqs", MAX_MCQS))
		except ValueError:
			num_mcqs = MAX_MCQS
			invalid_mcq_count = True
		try:
			num_short = int(request.form.get("num_short", MAX_SHORT_QUESTIONS))
			num_long = int(request.form.get("num_long", MAX_LONG_QUESTIONS))
		except ValueError:
			invalid_written_counts = True
		include_solutions = mode == "solutions"
		if invalid_mcq_count:
			error = f"Enter a whole number between 1 and {MAX_MCQS} for the MCQ count."
		elif invalid_written_counts:
			error = "Enter whole numbers for short- and long-question counts."
		elif mode not in {"questions", "solutions"}:
			error = "Choose either question paper only or question paper with solutions."
		elif subject not in SUBJECTS:
			error = "Choose Biology, Physics, Chemistry, or Computer Science."
		elif grade not in GRADES:
			error = "Choose Class 9 or Class 10."
		elif difficulty not in DIFFICULTIES:
			error = "Choose Easy, Medium, or Hard difficulty."
		elif not 1 <= num_mcqs <= MAX_MCQS:
			error = f"Choose between 1 and {MAX_MCQS} MCQs."
		elif not MIN_SHORT_QUESTIONS <= num_short <= MAX_SHORT_QUESTIONS or num_short % 2:
			error = f"Choose an even short-question count from {MIN_SHORT_QUESTIONS} to {MAX_SHORT_QUESTIONS}."
		elif not MIN_LONG_QUESTIONS <= num_long <= MAX_LONG_QUESTIONS or num_long % 2:
			error = f"Choose an even long-question count from {MIN_LONG_QUESTIONS} to {MAX_LONG_QUESTIONS}."
		elif generator not in {"offline", "ai"}:
			error = "Choose offline or AI question generation."
		elif generator == "offline":
			try:
				exam = build_offline_exam(subject, topic, include_solutions, grade, difficulty, num_mcqs, num_short, num_long)
				validate_exam(exam, include_solutions, num_mcqs, num_short, num_long)
				exam["saved_paper_id"] = save_paper_to_db(exam)
			except Exception as exc:
				app.logger.exception("Offline exam generation failed")
				error = str(exc) or "Offline generation failed. Please try again."
		elif not os.getenv("OPENAI_API_KEY"):
			error = "AI generation needs an OpenAI API key. Choose Offline generation to continue without a key or token."
		else:
			try:
				exam = build_exam(topic, include_solutions=include_solutions, subject=subject, grade=grade, difficulty=difficulty, num_mcqs_target=num_mcqs, num_short_target=num_short, num_long_target=num_long)
				exam["saved_paper_id"] = save_paper_to_db(exam)
			except Exception as exc:
				app.logger.exception("Exam generation failed")
				error = str(exc) or "The OpenAI request failed. Check your API key, network connection, and model access."
	return render_template("index.html", exam=exam, error=error, topic=topic, mode=mode, subject=subject, subjects=SUBJECTS, grade=grade, grades=GRADES, difficulty=difficulty, difficulties=DIFFICULTIES, generator=generator, num_mcqs=num_mcqs, num_short=num_short, num_long=num_long)


@app.route("/saved")
def saved_papers():
	with closing(sqlite3.connect(DATABASE_PATH)) as conn:
		conn.row_factory = sqlite3.Row
		papers = conn.execute(
			"SELECT id, title, subject, grade, difficulty, created_at FROM saved_papers ORDER BY id DESC"
		).fetchall()
	return render_template("saved.html", papers=papers)


@app.route("/download_pdf/<int:paper_id>")
def download_pdf(paper_id: int):
	with closing(sqlite3.connect(DATABASE_PATH)) as conn:
		row = conn.execute(
			"SELECT json_data FROM saved_papers WHERE id = ?", (paper_id,)
		).fetchone()
	if row is None:
		abort(404, description="Saved paper not found.")

	exam = json.loads(row[0])
	pdf_buffer = io.BytesIO()
	doc = SimpleDocTemplate(pdf_buffer, pagesize=letter)
	styles = getSampleStyleSheet()
	header_title = styles["Title"]
	header_title.alignment = 1
	header_title.fontSize = 19
	header_title.leading = 23
	section_heading = styles["Heading2"]
	section_heading.textColor = colors.HexColor("#17243b")
	table_text = styles["BodyText"]
	table_text.fontSize = 8
	table_text.leading = 10
	logo_path = os.path.join(app.root_path, "static", "school-logo.png")
	logo = Image(logo_path, width=68, height=68)
	header = Table(
		[[
			logo,
			[
				Paragraph("Scholar Public School", header_title),
				Paragraph("Exams", styles["Heading2"]),
				Paragraph(
					f"Federal Board (FBISE) | Total Marks: {exam['total_marks']:g}",
					styles["Normal"],
				),
			],
		]],
		colWidths=[82, doc.width - 82],
	)
	header.setStyle(TableStyle([
		("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
		("ALIGN", (0, 0), (0, 0), "CENTER"),
		("BOTTOMPADDING", (0, 0), (-1, -1), 10),
		("LINEBELOW", (0, 0), (-1, -1), 1.5, colors.HexColor("#17243b")),
	]))
	student_fields = Table(
		[
			[
				Paragraph("<b>Student Name:</b> ____________________", styles["Normal"]),
				Paragraph(f"<b>Class:</b> {escape(str(exam['grade']))}", styles["Normal"]),
			],
			[
				Paragraph(f"<b>Subject:</b> {escape(str(exam['subject']))}", styles["Normal"]),
				Paragraph(f"<b>Marks:</b> ____ / {exam['total_marks']:g}", styles["Normal"]),
			],
		],
		colWidths=[doc.width * 0.62, doc.width * 0.38],
	)
	student_fields.setStyle(TableStyle([
		("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
		("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#68758a")),
		("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#c5ceda")),
		("LEFTPADDING", (0, 0), (-1, -1), 8),
		("RIGHTPADDING", (0, 0), (-1, -1), 8),
		("TOPPADDING", (0, 0), (-1, -1), 8),
		("BOTTOMPADDING", (0, 0), (-1, -1), 8),
	]))
	story = [
		header,
		Spacer(1, 12),
		student_fields,
		Spacer(1, 14),
		Paragraph("Multiple-choice questions", section_heading),
	]
	mcq_rows = [[
		Paragraph("<b>No.</b>", table_text),
		Paragraph("<b>Question and options</b>", table_text),
		Paragraph("<b>Topic</b>", table_text),
		Paragraph("<b>Marks</b>", table_text),
	]]
	for question in exam["mcqs"]:
		question_content = [Paragraph(escape(str(question["question"])), table_text)]
		options_text = "&nbsp;&nbsp;&nbsp;&nbsp;".join(
			f"{escape(str(label))}. {escape(str(option))}"
			for label, option in question["options"].items()
		)
		question_content.append(Paragraph(options_text, table_text))
		if exam.get("solutions_included"):
			question_content.append(
				Paragraph(
					f"Answer: {escape(str(question.get('answer', '')))}. "
					f"{escape(str(question.get('solution', '')))}",
					table_text,
				)
			)
		mcq_rows.append([
			Paragraph(str(question["number"]), table_text),
			question_content,
			Paragraph(escape(str(question.get("topic", ""))), table_text),
			Paragraph("0.5", table_text),
		])
	mcq_table = Table(
		mcq_rows,
		colWidths=[35, doc.width - 35 - 95 - 40, 95, 40],
		repeatRows=1,
		hAlign="LEFT",
	)
	mcq_table.setStyle(TableStyle([
		("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf1ff")),
		("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#17243b")),
		("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#c5ceda")),
		("VALIGN", (0, 0), (-1, -1), "TOP"),
		("ALIGN", (0, 0), (0, -1), "CENTER"),
		("ALIGN", (-1, 0), (-1, -1), "CENTER"),
		("LEFTPADDING", (0, 0), (-1, -1), 5),
		("RIGHTPADDING", (0, 0), (-1, -1), 5),
		("TOPPADDING", (0, 0), (-1, -1), 5),
		("BOTTOMPADDING", (0, 0), (-1, -1), 5),
	]))
	story.append(mcq_table)
	story.append(Spacer(1, 12))

	for section_title, section_key in (
		("Short questions", "short_questions"),
		("Long questions", "long_questions"),
	):
		story.extend((Paragraph(section_title, styles["Heading2"]),))
		for question in exam[section_key]:
			if question.get("choice") == "B":
				story.append(Paragraph("OR", styles["Heading3"]))
			story.append(
				Paragraph(
					f"{question['number']}. ({escape(str(question['choice']))}) "
					f"{escape(str(question['question']))} "
					f"({question['marks']} marks)",
					styles["Normal"],
				)
			)
			if exam.get("solutions_included"):
				story.append(
					Paragraph(
						f"Model answer: {escape(str(question.get('solution', '')))}",
						styles["Normal"],
					)
				)
			story.append(Spacer(1, 5))

	doc.build(story)
	pdf_buffer.seek(0)
	filename = re.sub(r"[^A-Za-z0-9_-]+", "-", exam["title"]).strip("-") or "exams"
	return send_file(
		pdf_buffer,
		as_attachment=True,
		download_name=f"{filename}.pdf",
		mimetype="application/pdf",
	)


if __name__ == "__main__":
	app.run(debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")
