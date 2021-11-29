# configuration settings for the GUnicorn WSGI server that runs the Dash-based portal backend
bind = ['0.0.0.0:8050']
# uncomment next line only when debugging -- every client acccess request is logged to stdou
# accesslog = '-'
worker_class = 'sync'
workers = 5
threads = 1
timeout = 120
worker_tmp_dir = '/dev/shm'
