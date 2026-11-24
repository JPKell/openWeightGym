"""One shared connection used from several threads at once returns each thread its own whole rows."""
import threading

from db.database import database


def test_threads_sharing_a_connection_never_read_each_others_rows(tmp_path):
    connection = database(str(tmp_path / 'shared.db')).connect(check_same_thread=False)
    connection.execute('CREATE TABLE settings_changed (program TEXT, name TEXT, value TEXT)')
    with connection:
        for program in ('turnstile', 'loadcoach'):
            for index in range(40):
                connection.execute('INSERT INTO settings_changed VALUES (?, ?, ?)', (program, f'{program}-{index}', str(index)))
    problems, start = [], threading.Barrier(8)

    def reader(program):
        start.wait()
        for _ in range(150):
            # The same SQL text, so every thread uses the connection's one cached statement.
            rows = {name: value for name, value in connection.execute('SELECT name, value FROM settings_changed WHERE program = ?', (program,))}
            if len(rows) != 40 or not all(name.startswith(program) for name in rows):
                problems.append((program, len(rows)))
            if connection.execute('SELECT COUNT(*) FROM settings_changed WHERE program = ?', (program,)).fetchone() != (40,):
                problems.append((program, 'count'))

    threads = [threading.Thread(target=reader, args=(('turnstile', 'loadcoach')[index % 2],)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    connection.close()
    assert problems == []
