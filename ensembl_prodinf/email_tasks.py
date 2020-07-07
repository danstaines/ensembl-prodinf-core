import json
from celery.utils.log import get_task_logger
from celery.exceptions import Reject
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from ensembl_prodinf.email_celery_app import app
from ensembl_prodinf.utils import send_email


logger = get_task_logger(__name__)

smtp_server = app.conf['smtp_server']
from_email_address = app.conf['from_email_address']
retry_wait = app.conf['retry_wait']

http_adapter = HTTPAdapter(max_retries=Retry(total=3, backoff_factor=1,
                                             status_forcelist=[429, 500, 502, 503, 504],
                                             method_whitelist=["GET", "PUT", "POST", "DELETE"]))

def http_session():
    http = requests.Session()
    http.mount("http://", http_adapter)
    return http


@app.task(bind=True)
def email_when_complete(self, url, address):
    """ Task to check a URL and send an email once the result has a non-incomplete status
    Used for periodically checking whether a hive job has finished. If status is not complete,
    the task is retried
    Arguments:
      url - URL to check for job completion. Must return JSON containing status, subject and body fields
      address - address to send email
    """
    # allow infinite retries
    self.max_retries = None
    with http_session() as session:
        response = session.get(url)
    try:
        result = response.json()
    except json.JSONDecodeError as e:
        logger.error('Invalid response. URL: %s Status: %s Body: %s',
                     response.status_code, response.url, response.text)
        raise Reject(e, requeue=False)

    if (result['status'] == 'incomplete') or (result['status'] == 'running') or (result['status'] == 'submitted'):
        # job incomplete so retry task after waiting
        raise self.retry(countdown=retry_wait)
    # job complete so send email and complete task
    send_email(smtp_server=smtp_server, from_email_address=from_email_address, to_address=address, subject=result['subject'], body=result['body'])
    return result


@app.task(bind=True)
def email(self, address, subject, body):
    """ Simple task to send an email as specified
    Arguments:
      smtp_server
      from_email_address
      subject
      body
    """
    send_email(smtp_server=smtp_server,
               from_email_address=from_email_address,
               address=address,
               subject=subject,
               body=body)

