'''
Tasks and entrypoint need to accept and sequentially process a database. 
The data flow is:
1. handover_database (standard function)
- checks existence of database
- submits HCs if appropriate and submits celery task process_checked_db
- if not, submits copy and submits celery task process_copied_db
2. process_checked_db (celery task)
- wait/retry until healthcheck job has completed
- if success, submit copy job and submits celery task process_copied_db
3. process_copied_db (celery task)
- wait/retry until copy job has completed
- if success, submit metadata update job and submit celery task process_db_metadata
4. process_db_metadata (celery task)
- implementation incomplete
- plan is to wait for successful metadata update and then process event using a further endpoint
Step 1 is run in a client (e.g a flask endpoint), all subsequent steps are run in celery workers
@author: dstaines
'''
from ensembl_prodinf.handover_celery_app import app

import logging 
import db_copy_client
import hc_client
from sqlalchemy_utils.functions import database_exists
from sqlalchemy.engine.url import make_url
from .utils import send_email
import handover_config as cfg
import uuid     
import re

logger = logging.getLogger('handover')
logger.setLevel(logging.DEBUG)
# TODO set message-based appenders
                
def handover_database(spec):    
    """ Method to accept a new database for incorporation into the system 
    Argument is a dict with the following keys:
    * src_uri - URI to database to handover (required) 
    * tgt_uri - URI to copy database to (optional - generated from staging and src_uri if not set)
    * contact - email address of submitter (required)
    * type - string describing type of update (required)
    * comment - additional information about submission (required)
    The following keys are added during the handover process:
    * handover_token - unique identifier for this particular handover invocation
    * hc_job_id - job ID for healthcheck process
    * db_job_id - job ID for database copy process
    """
    # TODO verify dict
    logging.info("Handling " + str(spec))
    if 'tgt_uri' not in spec:
        spec['tgt_uri'] = get_tgt_uri(spec['src_uri'])
    spec['handover_token'] = str(uuid.uuid1())                    
    check_db(spec['src_uri'])
    groups = groups_for_uri(spec['src_uri'])
    if(groups==None):
        logger.info("No HCs needed, starting copy")
        submit_copy(spec)
    else: 
        logger.info("Starting HCs")
        submit_hc(spec, groups)
    return spec['handover_token']

def get_tgt_uri(src_uri):
    """Create target URI from staging details and name of source database"""
    url = make_url(src_uri)
    return str(cfg.staging_uri) + str(url.database)

    
def check_db(uri):    
    """Check if source database exists"""
    if(database_exists(uri) == False):
        logging.error(uri + " does not exist")
        raise ValueError(uri + " does not exist")
    else:
        logging.info(uri + " looks good to me")
        return


core_pattern = re.compile(".*[a-z]_(core|rnaseq|cdna|otherfeatures)_[0-9].*")   
variation_pattern = re.compile(".*[a-z]_variation_[0-9].*")   
compara_pattern = re.compile(".*[a-z]_compara_[0-9].*")   
funcgen_pattern = re.compile(".*[a-z]_funcgen_[0-9].*")
def groups_for_uri(uri):
    """Find which HC group to run on a given database"""
    if(core_pattern.match(uri)):
        return [cfg.core_handover_group]
    elif(variation_pattern.match(uri)):
        return [cfg.variation_handover_group]
    elif(funcgen_pattern.match(uri)):
        return [cfg.funcgen_handover_group]
    elif(compara_pattern.match(uri)):
        return [cfg.compara_handover_group]
    else:
        return None


def submit_hc(spec, groups):
    """Submit the source database for healthchecking. Returns a celery job identifier"""
    hc_job_id = hc_client.submit_job(cfg.hc_uri, spec['src_uri'], cfg.production_uri, cfg.compara_uri, cfg.staging_uri, cfg.live_uri, None, groups, cfg.data_files_path, None)
    spec['hc_job_id'] = hc_job_id
    task_id = process_checked_db.delay(hc_job_id, spec)
    logging.debug("Submitted DB for checking as " + str(task_id))
    send_email(to_address=spec['contact'], subject='HC submitted', body=str(spec['src_uri']) + ' has been submitted for checking', smtp_server=cfg.smtp_server)
    return task_id

@app.task(bind=True)
def process_checked_db(self, hc_job_id, spec):
    """ Task to wait until HCs finish and then respond e.g.
    * submit copy if HCs succeed
    * send error email if not
    """
    
    # allow infinite retries 
    self.max_retries = None
    logging.info("Checking HCs for " + spec['src_uri'] + " from job " + str(hc_job_id))
    result = hc_client.retrieve_job(cfg.hc_uri, hc_job_id)
    if (result['status'] == 'incomplete') or (result['status'] == 'running') or (result['status'] == 'submitted'):
        logging.info("Job incomplete, retrying")
        raise self.retry()
    
    # check results
    if (result['status'] == 'failed'):
        logging.info("HCs failed to run")    
        msg = """
Running healthchecks vs %s failed to execute.
Please see %s
""" % (spec['src_uri'], cfg.hc_web_uri + str(hc_job_id))
        send_email(to_address=spec['contact'], subject='HC failed to run', body=msg, smtp_server=cfg.smtp_server)         
        return 
    elif (result['output']['status'] == 'failed'):
        logging.info("HCs found problems")
        msg = """
Running healthchecks vs %s completed but found failures.
Please see %s
""" % (spec['src_uri'], cfg.hc_web_uri + str(hc_job_id))
        send_email(to_address=spec['contact'], subject='HC ran but failed', body=msg, smtp_server=cfg.smtp_server)  
        return
    else:
        logging.info("HCs fine, starting copy")
        submit_copy(spec)


def submit_copy(spec):
    """Submit the source database for copying to the target. Returns a celery job identifier"""
    copy_job_id = db_copy_client.submit_job(cfg.copy_uri, spec['src_uri'], spec['tgt_uri'], None, None, False, True, None)
    spec['copy_job_id'] = copy_job_id
    task_id = process_copied_db.delay(copy_job_id, spec)    
    logging.debug("Submitted DB for copying as " + str(task_id))
    return task_id


@app.task(bind=True)    
def process_copied_db(self, copy_job_id, spec):
    """Wait for copy to complete and then respond accordingly:
    * if success, submit to metadata database
    * if failure, flag error using email"""
    # allow infinite retries 
    self.max_retries = None
    logging.info("Checking " + str(spec) + " using " + str(copy_job_id))
    result = db_copy_client.retrieve_job(cfg.copy_uri, copy_job_id)
    if (result['status'] == 'incomplete') or (result['status'] == 'running') or (result['status'] == 'submitted'):
        logging.info("Job incomplete, retrying")
        raise self.retry() 
    
    if (result['status'] == 'failed'):
        logging.info("Copy failed")
        msg = """
Copying %s to %s failed.
Please see %s
""" % (spec['src_uri'], spec['tgt_uri'], cfg.copy_web_uri + str(copy_job_id))
        send_email(to_address=spec['contact'], subject='Database copy failed', body=msg, smtp_server=cfg.smtp_server)          
        return
    else:
        logging.info("Copying complete, need to submit metadata")
        meta_job_id = submit_metadata_update(spec['tgt_uri'])
        task_id = process_db_metadata.delay(meta_job_id, spec)    
        logging.debug("Submitted DB for meta update as " + str(task_id))
        return task_id
    

def submit_metadata_update(uri):
    """Submit the source database for copying to the target. Returns a celery job identifier. Currently not implemented"""
    # TODO submit metadata job once complete
    return 'metaplaceholder_db_id'


@app.task(bind=True)    
def process_db_metadata(self, meta_job_id, spec):
    """Wait for metadata update to complete and then respond accordingly:
    * if success, submit event to event handler for further processing
    * if failure, flag error using email"""
    logging.info("Assuming completed meta update " + str(meta_job_id))
    # TODO wait for job to complete
    # TODO retrieve event from metadata job
    # TODO pass event to event handler endpoint to trigger more processing
    return