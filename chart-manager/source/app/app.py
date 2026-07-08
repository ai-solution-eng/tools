from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
import requests
import json
import io
import sqlite3
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'super_secret_key'  # For session

# SQLite DB for logs
conn = sqlite3.connect('audit_logs.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, action TEXT, details TEXT)''')
conn.commit()

def log_action(action, details):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute("INSERT INTO logs (timestamp, action, details) VALUES (?, ?, ?)", (timestamp, action, details))
    conn.commit()

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == 'chartadmin' and password == 'chartpassword':
            session['logged_in'] = True
            log_action('Login', f"User {username} logged in")
            return redirect(url_for('index'))
        else:
            flash('Invalid credentials. Try again.')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    session.pop('chartmuseum_url', None)
    flash('Logged out successfully.')
    return redirect(url_for('login'))

# Middleware to check auth
@app.before_request
def require_login():
    allowed_routes = ['login']
    if 'logged_in' not in session and request.endpoint not in allowed_routes:
        return redirect(url_for('login'))

@app.route('/', methods=['GET', 'POST'])
def index():
    if 'chartmuseum_url' in session:
        return redirect(url_for('manage'))
    if request.method == 'POST':
        ip = request.form.get('chartmuseum_ip')
        if ip:
            session['chartmuseum_url'] = f"http://{ip}:8080"
            log_action('IP Set', f"ChartMuseum IP set to {ip}")
            return redirect(url_for('manage'))
        else:
            flash('Please enter a valid IP.')
    return render_template('index.html')

@app.route('/change_address')
def change_address():
    session.pop('chartmuseum_url', None)
    return redirect(url_for('index'))

@app.route('/manage', methods=['GET', 'POST'])
def manage():
    if 'chartmuseum_url' not in session:
        return redirect(url_for('index'))
    
    url = session['chartmuseum_url']
    charts = []
    error = None
    search_query = request.args.get('search', '').lower()
    since = request.args.get('since', '')
    until = request.args.get('until', '')
    min_version = request.args.get('min_version', '')
    page = int(request.args.get('page', 1))
    size = int(request.args.get('size', 10))
    
    # List charts
    try:
        response = requests.get(f"{url}/api/charts")
        response.raise_for_status()
        all_charts = response.json()
        filtered_charts = {}
        for name, versions in all_charts.items():
            filtered_versions = versions
            if search_query:
                filtered_versions = [v for v in filtered_versions if search_query in name.lower() or search_query in v.get('version', '').lower() or search_query in v.get('description', '').lower()]
            if min_version:
                filtered_versions = [v for v in filtered_versions if v.get('version', '0') >= min_version]
            if since:
                since_date = datetime.strptime(since, '%Y-%m-%d').date()
                filtered_versions = [v for v in filtered_versions if datetime.fromisoformat(v.get('created', '1900-01-01T00:00:00Z').replace('Z', '+00:00')).date() >= since_date]
            if until:
                until_date = datetime.strptime(until, '%Y-%m-%d').date()
                filtered_versions = [v for v in filtered_versions if datetime.fromisoformat(v.get('created', '1900-01-01T00:00:00Z').replace('Z', '+00:00')).date() <= until_date]
            if filtered_versions:
                filtered_charts[name] = filtered_versions
        charts = filtered_charts
        
        # Custom pagination (server-side slice)
        flat_charts = []
        for name, versions in charts.items():
            for v in versions:
                v['name'] = name  # Add name to flat list
                flat_charts.append(v)
        total = len(flat_charts)
        start = (page - 1) * size
        end = start + size
        flat_charts = flat_charts[start:end]
        
    except Exception as e:
        error = str(e)
    
    # Multi-file upload
    if request.method == 'POST':
        files = request.files.getlist('chart_file')
        for file in files:
            if file and file.filename:
                try:
                    response = requests.post(f"{url}/api/charts", files={'chart': file})
                    response.raise_for_status()
                    flash(f'Chart {file.filename} uploaded successfully!')
                    log_action('Upload', f"Uploaded chart {file.filename}")
                except Exception as e:
                    flash(f'Upload of {file.filename} failed: {str(e)}')
        return redirect(url_for('manage'))
    
    return render_template('manage.html', flat_charts=flat_charts, error=error, chartmuseum_url=url, search_query=search_query, since=since, until=until, min_version=min_version, page=page, size=size, total=total)

@app.route('/delete/<name>/<version>')
def delete(name, version):
    if 'chartmuseum_url' not in session:
        return redirect(url_for('index'))
    
    url = session['chartmuseum_url']
    try:
        response = requests.delete(f"{url}/api/charts/{name}/{version}")
        response.raise_for_status()
        flash('Chart version deleted successfully!')
        log_action('Delete', f"Deleted {name}/{version}")
    except Exception as e:
        flash(f'Delete failed: {str(e)}')
    return redirect(url_for('manage'))

@app.route('/download/<name>/<version>')
def download(name, version):
    if 'chartmuseum_url' not in session:
        return redirect(url_for('index'))
    
    url = session['chartmuseum_url']
    try:
        response = requests.get(f"{url}/api/charts/{name}/{version}")
        response.raise_for_status()
        file_data = io.BytesIO(response.content)
        log_action('Download', f"Downloaded {name}/{version}")
        return send_file(file_data, as_attachment=True, download_name=f"{name}-{version}.tgz")
    except Exception as e:
        flash(f'Download failed: {str(e)}')
        return redirect(url_for('manage'))

@app.route('/logs')
def logs():
    cursor.execute("SELECT timestamp, action, details FROM logs ORDER BY id DESC")
    logs = cursor.fetchall()
    return render_template('logs.html', logs=logs)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)