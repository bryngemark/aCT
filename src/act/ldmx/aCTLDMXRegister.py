from datetime import timedelta
import json
import os
import shutil
from urllib.parse import urlparse

from rucio.client import Client
from rucio.common.exception import RucioException, DataIdentifierNotFound

from act.ldmx.aCTLDMXProcess import aCTLDMXProcess

class aCTLDMXRegister(aCTLDMXProcess):
    '''
    Post-processing for LDMX jobs. Reads metadata json file and registers output
    files in Rucio.
    '''

    def __init__(self):

        aCTLDMXProcess.__init__(self)
        self.rucio = Client()

 
    def processDoneJobs(self):
        '''
        Look for done jobs, and register output metadata in Rucio
        '''

        select = "arcstate='done' and arcjobs.id=ldmxjobs.arcjobid"
        columns = ['arcjobs.id', 'JobID', 'appjobid', 'cluster', 'UsedTotalWallTime',
                   'arcjobs.EndTime', 'stdout', 'ldmxjobs.created', 'description', 'template']
        arcjobs = self.dbarc.getArcJobsInfo(select, columns=columns, tables='arcjobs,ldmxjobs')
        if not arcjobs:
            return

        for aj in arcjobs:
            self.log.info(f'Found finished job {aj["id"]}')
            jobid = aj.get('JobID')
            if not jobid:
                self.log.error(f'No JobID in arcjob {aj["id"]}')
                continue

            # copy to joblog dir files downloaded for the job: gmlog errors and job stdout
            self.copyOutputFiles(aj)
 
            # Read the metadata and insert into rucio
            select = f"id={int(aj['appjobid'])}"
            desc = {'computingelement': aj['cluster'],
                    'sitename': self.endpoints[aj['cluster']],
                    'starttime': aj['EndTime'] - timedelta(0, aj['UsedTotalWallTime']),
                    'endtime': aj['EndTime']}
            if self.insertMetadata(aj):
                desc['ldmxstatus'] = 'finished'
            else:
                desc['ldmxstatus'] = 'failed'
            self.dbldmx.updateJobsLazy(select, desc)

            # Clean tmp dir
            self.cleanDownloadedJob(jobid)

            # Set arc job to clean
            select = f"id={aj['id']}"
            desc = {"arcstate": "toclean", "tarcstate": self.dbarc.getTimeStamp()}
            self.dbarc.updateArcJobsLazy(desc, select)

            # Clean input files
            self.cleanInputFiles(aj)

        self.dbldmx.Commit()
        self.dbarc.Commit()

    def copyOutputFiles(self, arcjob):
        '''
        Copy job stdout and errors log to final location
        '''
        sessionid = arcjob['JobID'][arcjob['JobID'].rfind('/')+1:]
        date = arcjob['created'].strftime('%Y-%m-%d')
        outd = os.path.join(self.conf.get(['joblog','dir']), date)
        os.makedirs(outd, 0o755, exist_ok=True)

        localdir = os.path.join(self.tmpdir, sessionid)
        gmlogerrors = os.path.join(localdir, "gmlog", "errors")
        arcjoblog = os.path.join(outd, "%s.log" % arcjob['id'])
        try:
            shutil.move(gmlogerrors, arcjoblog)
            os.chmod(arcjoblog, 0o644)
        except Exception as e:
            self.log.error(f'Failed to copy {gmlogerrors}: {e}')

        jobstdout = arcjob['stdout']
        if jobstdout:
            try:
                shutil.move(os.path.join(localdir, jobstdout),
                            os.path.join(outd, '%s.out' % arcjob['id']))
                os.chmod(os.path.join(outd, '%s.out' % arcjob['id']), 0o644)
            except Exception as e:
                self.log.error(f'Failed to copy file {os.path.join(localdir, jobstdout)}, {str(e)}')


    def cleanInputFiles(self, job):
        '''
        Clean job input files in tmp dir
        '''
        try:
            os.remove(job['description'])
            os.remove(job['template'])
            self.log.debug(f'Removed {job["description"]} and {job["template"]}')
        except:
            pass


    def insertMetadata(self, arcjob):
        '''
        Read metadata file and insert into Rucio
        '''
        sessionid = arcjob['JobID'][arcjob['JobID'].rfind('/')+1:]
        metadatafile = os.path.join(self.tmpdir, sessionid, 'rucio.metadata')
        try:
            with open(metadatafile) as f:
                metadata = json.load(f)
        except Exception as e:
            self.log.error(f'Failed to read metadata.json file at {metadatafile}: {e}')
            return False

        # Set RSE from configuration
        metadata['rse'] = self.sites[self.endpoints[arcjob['cluster']]]['rse']
        # Set some aCT metadata
        metadata['ComputingElement'] = urlparse(arcjob['cluster']).hostname or 'unknown'
        metadata['JobSubmissionTime'] = arcjob['created']
        try:
            scope = metadata['scope']
            name = metadata['name']
            dscope = metadata['datasetscope']
            dname = metadata['datasetname']
            self.log.info(f'Inserting metadata info for {scope}:{name}: {metadata}')
            # Add replica
            pfn = f'file://{metadata["DataLocation"]}'
            self.rucio.add_replica(metadata['rse'], scope, name, metadata['bytes'],
                                   None, pfn=pfn, md5=metadata['md5'])
            try:
                # Attach to dataset
                self.rucio.attach_dids(dscope, dname, [{'scope': scope, 'name': name}])
            except DataIdentifierNotFound:
                try:
                    self.rucio.add_dataset(dscope, dname)
                except RucioException as e:
                    self.log.error(f'Dataset {dscope}:{dname} does not exist and failed to create it: {e}')
                else:
                    self.rucio.attach_dids(dscope, dname, [{'scope': scope, 'name': name}])

            # Add metadata, removing all rucio "native" metadata
            native_metadata = ['scope', 'name', 'bytes', 'md5', 'rse',
                               'datasetscope', 'datasetname']
            # Metadata values must be strings to be searchable
            self.rucio.add_did_meta(scope, name,
                                    {x: str(y) for x, y in metadata.items() if x not in native_metadata})
        except KeyError as e:
            self.log.info(f'key missing in metadata json: {e}')
            return False
        except RucioException as e:
            self.log.warning(f'Rucio exception: {e}')
            return False

        return True


    def cleanDownloadedJob(self, arcjobid):
        '''
        Remove directory to which job was downloaded.
        '''

        sessionid = arcjobid[arcjobid.rfind('/')+1:]
        localdir = os.path.join(self.tmpdir, sessionid)
        self.log.debug(f'Removing directory {localdir}')
        shutil.rmtree(localdir, ignore_errors=True)


    def process(self):

        # Look for done jobs and process the metadata
        self.processDoneJobs()


if __name__ == '__main__':

    ar = aCTLDMXRegister()
    ar.run()
    ar.finish()
