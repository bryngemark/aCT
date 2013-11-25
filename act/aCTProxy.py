import os
import logging
import aCTConfig
from aCTDBArc import aCTDBArc
import datetime, time
import arc

class aCTProxy:

    def __init__(self, Interval=3600):
        self.interval = Interval
        self.conf=aCTConfig.aCTConfigARC()
        self.db=aCTDBArc(logging.getLogger(), self.conf.get(["db","file"]))
        cred_type=arc.initializeCredentialsType(arc.initializeCredentialsType.SkipCredentials)
        self.uc=arc.UserConfig(cred_type)
        self.uc.CACertificatesDirectory(str(self.conf.get(["voms", "cacertdir"])))
        self.__initrobotproxies__()
        self.tstamp = datetime.datetime.utcnow()
        
    def __initrobotproxies__(self):
        '''
        Initialize pilot and production proxies generated from robot proxy. The proxies
        are assumed to be generated by an external cron and put under proxydir, defined in
        voms part of the ARC config file. The pilot and production proxies are assumed to 
        come from the same DN.
        '''
        self.proxypaths = {}
        self.proxypaths["pilot"] = os.path.join(self.conf.get(["voms","proxydir"]), "pilot_x509up.proxy")
        self.proxypaths["production"]  = os.path.join(self.conf.get(["voms","proxydir"]), "prod_x509up.proxy")
        
        for role, path in self.proxypaths.items():
            proxy, self.robodn, expirytime = self._readProxyFromFile(path)
            self.updateProxy(proxy, self.robodn, role, expirytime)
        
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
        dn = cred.GetDN()
        expirytime=datetime.datetime.strptime(str(cred.GetEndTime()),"%Y-%m-%d %H:%M:%S")
        timeleft = self._timediffSeconds(expirytime, datetime.datetime.utcnow())
        if timeleft <= 0:
            raise Exception("Failed in importing proxy: "+path+" has expired!")
        return proxy, dn, expirytime

    
    def updateProxy(self, proxy, dn, role, expirytime):
        '''
        Update proxy of given dn/role. If no previous proxy, do insert instead.
        '''
        proxyid = self.getProxyInfo(dn, role, columns=["id"])
        if not proxyid:
            self.db.insertProxy(proxy, dn, str(expirytime), role=role)
        else:
            desc={}
            desc["proxy"]=proxy
            desc["dn"]=dn
            desc["expirytime"]=str(expirytime)
            desc["role"]=role
            self.db.updateProxy(proxyid["id"], desc)

    def renew(self):
        "renews proxies in db. currently only robot production and pilot proxies are renewed."
        t=datetime.datetime.utcnow()
        if self._timediffSeconds(t, self.tstamp) < self.interval:
            return
        self.tstamp=t
        for role, path in self.proxypaths.items():
            tleft = self.timeleft(self.robodn, role)
            if tleft <= int(self.conf.get(["voms","minlifetime"])) :
                proxy, _, expirytime = self._readProxyFromFile(path)
                self.updateProxy(proxy, self.robodn, role, expirytime)
                if tleft == 0:
                    raise Exception("VOMS proxy not extended")
    
    def getProxyInfo(self, dn, role, columns=[]):
        """
        get info on proxy with given dn and role in proxies table. Returns dict with entries
        corresponding to columns, or all columns if no columns are given. 
        """
        select = "dn='"+dn+"' and role='"+role+"'"
        ret_columns = self.db.getProxiesInfo(select, columns, expect_one=True)
        return ret_columns
            
    def timeleft(self, dn, role):
        expirytime = self.getProxyInfo(dn, role, ["expirytime"])
        if "expirytime" in expirytime and expirytime["expirytime"]:
            total_seconds = self._timediffSeconds(expirytime["expirytime"], datetime.datetime.utcnow())
            return total_seconds
        else:
            return 0

    def path(self, dn, role):
        proxypath = self.getProxyInfo(dn, role, columns=["proxypath"])
        return proxypath["proxypath"]

def test_aCTProxy():
    p=aCTProxy(1)
    _, dn, expirytime = p._readProxyFromFile(os.path.join(p.conf.get(["voms","proxydir"]), "pilot_x509up.proxy"))
    print p.path(dn, "production")
    time.sleep(30)
    p.renew()

if __name__ == '__main__':
    test_aCTProxy()

