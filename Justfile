set shell := ["bash", "-cu"]

# Помощь по тому, что вообще умеет
help:
	just --list

# Установка зависимостей в активное uv-окружение
install:
	uv pip install -r requirements.txt

# Запуск сервера (слушает на всех интерфейсах)
run:
	uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Экспорт результатов в XLSX с временным именем
export:
	ts=$(date +"report-%y-%m-%d-%H-%M-%S.xlsx"); \
	echo "Writing $ts"; \
	uv run python export_results.py -o "$ts"

# Сохранить копию БД и удалить оригинал (<QUIZ_FILE>.db)
rmdb:
	json_file=${QUIZ_FILE:-test.json}; \
	db_file="${json_file%.json}.db"; \
	if [ -f "$db_file" ]; then \
		cp "$db_file" "${db_file}.bak.$(date +%s)"; \
		rm "$db_file"; \
		echo "Deleted $db_file (backup created)"; \
	else \
		echo "DB not found: $db_file"; \
	fi
