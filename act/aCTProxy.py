import os
import logging
import aCTConfig
from aCTDBArc import aCTDBArc
import datetime, time
import arc
import subprocess
import re

class aCTProxy:

    def __init__(self, logger, Interval=3600):
        self.interval = Interval
        self.conf=aCTConfig.aCTConfigARC()
        self.db=aCTDBArc(logger, self.conf.get(["db","file"]))
        self.log = logger
        cred_type=arc.initializeCredentialsType(arc.initializeCredentialsType.SkipCredentials)
        self.uc=arc.UserConfig(cred_type)
        self.uc.CACertificatesDirectory(str(self.conf.get(["voms", "cacertdir"])))
        self.tstamp = datetime.datetime.utcnow()
        self.voms_proxies = {}
                
    def _timediffSeconds(self, t1, t2):
        '''
        Helper function. Takes datetime.datetime t1 and t2, returns t1-t2 in seconds
        '''
        return time.mktime(t1.timetuple())-time.mktime(t2.timetuple())
    
    def _readProxyFromFile(self, path):
        f = open(path)
        proxy = f.read()
        f.close()
        self.uc.CredentialString(proxy)
        cred=arc.Credential(self.uc)
        dn = cred.GetIdentityName()
        expirytime=datetime.datetime.strptime(cred.GetEndTime().str(arc.UTCTime),"%Y-%m-%dT%H:%M:%SZ")
        return proxy, dn, expirytime

    def _createVomsProxyFromFile(self, oldproxypath, newproxypath, validHours, voms, attribute=''):
        '''
        Helper function to create proxy under newproxypath from proxy under oldproxypath
        with given voms and attribute (if given), using arcproxy. 
        '''
        cmd=[self.conf.get(["voms","bindir"])+"/arcproxy"]
        cmd.extend(["--constraint=validityPeriod="+str(validHours)+"H"])
        cmd.extend(["--constraint=vomsACvalidityPeriod="+str(validHours)+"H"])
        cmd.extend(["--voms="+voms])
        if attribute:
            cmd[-1]+=":"+attribute
        cmd.extend(["--cert="+oldproxypath])
        cmd.extend(["--key="+oldproxypath])
        cmd.extend(["--proxy="+newproxypath])
        
        p = subprocess.call(cmd)
        return p

    def createVOMSAttribute(self, voms, attribute, proxypath="", validHours=96, proxyid=None):
        '''
        Function to create proxy with voms extensions from proxy.
        Example: To add production attribute to atlas voms, set voms="atlas" and 
        attribute="/atlas/Role=production". The proxy file under proxypath will
        be used to generate/update the proxy.
        If proxyid is None, a new proxy entry will be created.
        After a call to this function, the new proxy will be automatically renewed 
        with a call to the renew() function.
        '''
        if not proxypath:
            proxypath=self.conf.get(["voms", "proxypath"])
        _, dn, expirytime = self._readProxyFromFile(proxypath)
        # if not given, try to get proxyid using dn and attribute first
        if not proxyid:
            proxyid = self.getProxyId(dn, attribute)
        # if still no proxyid, a new proxies table entry must be created
        if not proxyid:
            proxyid = self.updateProxy("", dn, attribute, expirytime)
        dbproxypath = self.db.getProxyPath(proxyid)
        retries = 3
        while self._createVomsProxyFromFile(proxypath, dbproxypath, validHours, voms, attribute):
            # todo: check that attribute is actually set in the new proxy.
            retries -= 1
            if retries == 0:
                self.log.warning("Got errors when creating VOMS proxy from file %s", proxypath)
                break
            #give arcproxy a bit of time before retrying
            time.sleep(1)
        proxy, _, expirytime = self._readProxyFromFile(dbproxypath)
        desc={"proxy":proxy, "expirytime":expirytime}
        self.db.updateProxy(proxyid, desc)
        self.voms_proxies[(dn, attribute)] = (voms, attribute, proxypath, validHours, proxyid)
        return proxyid
    
    def deleteVOMSRole(self, dn, attribute):
        '''
        Function to remove proxy with VOMS extension generated by createVOMSRole. 
        It's advisable to check that no jobs depend on this proxy before
        calling this function.
        '''
        if not self.voms_proxies.has_key((dn, attribute)):
            self.log.error("Cannot delete voms proxy with dn %s and attribute %s.", dn, attribute)
        (_, _, _, _, proxyid) = self.voms_proxies[(dn, attribute)]
        self.db.deleteProxy(proxyid)
        del(self.voms_proxies[(dn, attribute)])
    
    def updateProxy(self, proxy, dn, attribute, expirytime):
        '''
        Update proxy of given dn/attribute. If no previous proxy, do insert instead.
        '''
        try:
            proxyid = self.getProxyInfo(dn, attribute, columns=["id"])["id"]
        except:
            proxyid = None
        if not proxyid:
            proxyid = self.db.insertProxy(proxy, dn, str(expirytime), attribute=attribute)
        else:
            desc={}
            desc["proxy"]=proxy
            desc["dn"]=dn
            desc["expirytime"]=str(expirytime)
            desc["attribute"]=attribute
            self.db.updateProxy(proxyid, desc)
        return proxyid

    def renew(self):
        "renews proxies in db. renews all proxies created with createVOMSRole."
        t=datetime.datetime.utcnow()
        if self._timediffSeconds(t, self.tstamp) < self.interval:
            return
        self.tstamp=t
        for (dn, attribute), args in self.voms_proxies.items():
            tleft = self.timeleft(dn, attribute)
            if tleft <= int(self.conf.get(["voms","minlifetime"])) :
                self.createVOMSAttribute(*args)
                tleft = self.timeleft(dn, attribute)
                if tleft <= 0:
                    self.log.error("VOMS proxy not extended")
    
    def getProxyInfo(self, dn, attribute, columns=[]):
        """
        get info on proxy with given dn and attribute in proxies table. Returns dict with entries
        corresponding to columns, or all columns if no columns are given. 
        """
        select = "dn='"+dn+"' and attribute='"+attribute+"'"
        ret_columns = self.db.getProxiesInfo(select, columns, expect_one=True)
        return ret_columns

    def getProxyId(self, dn, attribute):
        id = self.getProxyInfo(dn, attribute, ["id"])
        if id:
            return id["id"]
        else:
            return None

    def timeleft(self, dn, attribute):
        expirytime = self.getProxyInfo(dn, attribute, ["expirytime"])
        if "expirytime" in expirytime and expirytime["expirytime"]:
            total_seconds = self._timediffSeconds(expirytime["expirytime"], datetime.datetime.utcnow())
            return total_seconds
        else:
            return 0

    def path(self, dn='', attribute='', id=''):
        if id:
            proxypath = self.db.getProxyPath(id)
        else:
            proxypath = self.getProxyInfo(dn, attribute, columns=["proxypath"])
            if not proxypath:
                self.log.warning("No proxy found for DN %s and attribute %s" % (dn, attribute))
                return None
            proxypath = proxypath["proxypath"]
        return proxypath

def test_aCTProxy():
    p=aCTProxy(logging.getLogger(), 1)
    voms="atlas"
    attribute="/atlas/Role=pilot"
    proxypath=p.conf.get(["voms", "proxypath"])
    validHours=12
    proxyid = p.createVOMSAttribute(voms, "/atlas/Role=pilot", proxypath, validHours)
    proxyid = p.createVOMSAttribute(voms, "/atlas/Role=production", proxypath, validHours)
    dn = p.db.getProxiesInfo("id="+str(proxyid), ["dn"], expect_one=True)["dn"]
    print "dn=", dn
    print "path from dn,attribute lookup matches path from proxyid lookup:", 
    print p.path(dn=dn, attribute=attribute) == p.path(id=p.getProxyId(dn, attribute))
    time.sleep(30)
    p.renew()

if __name__ == '__main__':
    test_aCTProxy()

