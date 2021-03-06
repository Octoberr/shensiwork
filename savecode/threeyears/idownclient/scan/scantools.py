"""
拿到任务后按着插件执行顺序开始执行
modify by judy 2020/03/18

新增回馈 by judy 2020/04/08
暂时增加断点续扫的功能，目前只简单的增加以下
记录扫描到了哪个port
"""
from pathlib import Path
import re
import threading
import time
import traceback
import uuid
from queue import Empty, Queue
import IPy

from datacontract import EClientBusiness
from ..config_client import basic_client_config

clientbusiness = eval(basic_client_config.clientbusiness)
if (
    EClientBusiness.ALL.value in clientbusiness
    or EClientBusiness.IScanTask.value in clientbusiness
):
    from geoiploc.geoiploc import GeoIPLoc

from datacontract import IscanTask, ECommandStatus
from idownclient.config_scanner import (
    max_nmap_ip,
    max_nscan_threads,
    max_zscan_threads,
    max_zgrab2_threads,
    max_zscan_ipranges,
    max_zscan_ip,
    max_vulns_threads,
)
from .ipprocesstools import IpProcessTools
from .plugin.dbip.dbipmmdb import DbipMmdb
from .plugin.logicalbanner import LogicalGrabber
from .plugin.nmap.nmap import Nmap
from .plugin.zgrab2.zgrab2 import Zgrab2
from .plugin.zmap.zmap import Zmap

# from .plugin.masscan.masscan import Masscan
from .scanplugbase import ScanPlugBase
from ..clientdatafeedback.scoutdatafeedback import IP, PortInfo, GeoInfo


class ScanTools(ScanPlugBase):
    def __init__(self, task: IscanTask):
        ScanPlugBase.__init__(self, task)
        self.task: IscanTask = task
        self.nmap = Nmap()
        self.zgrab2 = Zgrab2()
        self.zmap = Zmap()  # zmap和masscan的功能一样的，现在尝试使用masscan来试下呢
        self.logicalgrabber = LogicalGrabber()
        # 新增查询ip归属地，modify by judy 2020/03/31
        self.dbip = DbipMmdb()
        # 初始化的时候去拿cmd里面的host，因为支持了国家二字码，所以单独开一个方法来获取
        self.re_iprang = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\/\d{2}")
        # 新增所有C段ip计数，用于计算进度
        self.__c_ipranges_count = 0
        self.__all_scan_count = 0
        # 已经扫描了的ip段统计
        self.__has_scan_count = 0
        # 目前的扫描进度:98.99 %
        self.__progress: float = 0.00
        # 端口队列，用于多线程调用zmap
        self.port_queue = Queue()
        # 使用字典，便于去重，modify by judy 2020/08/06
        # 为了提高效率，将单个ip和C段IP分开处理
        self.hosts = {}
        self.host = {}
        # 文件处理锁，可能会出现同名的文件所以需要加文件锁 modify by judy 20210203
        self._file_locker = threading.RLock()
        # zmap 待处理的队列，默认为运行状态
        self.make_zmap_scan_queue_status = True
        # zmap处理队列
        self.zmap_queue = Queue()
        self.__zmap_scan_thread_state = {}

        # namp处理的队列
        self.nmap_queue = Queue()
        self.__nmap_scan_thread_state = {}
        self._nmap_tmp_dict_locker = threading.RLock()
        self._nmap_tmp = {}

        # zgrab2处理的队列
        self.zgrab2_queue = Queue()
        self.__zgrab2_scan_thread_state = {}

        # 20200917新增的漏洞扫描需求要求匹配漏洞并扫描
        self.vulns_queue = Queue()
        self.__vulns_scan_thread_state = {}
        self._vulns_list = self.task.cmd.stratagyscan.scan.vuls

        # 最后的结果线程
        self.output_res_queue = Queue()
        # 目前有两种数据来源，一种是手动输入的；另外一种是查询国家得到的ip段，
        # 因为查询国家得到的ip段比较多，所以需要一个标志来分辨下
        # 默认是手动数据，数据来源为国家数据时，则得到的结果一定是seres对象
        self.country_flag = False
        # 保存端口扫描进度
        self.sp = Path("./scan_port_progress.txt")

    def process_host(self):
        """
        处理传入的需要扫描的host
        :return:
        """
        log = "开始处理IP段"
        self._logger.debug("Start process ip ranges")
        self._outprglog(log)
        cmdhost: list = self.task.cmd.stratagyscan.scan.hosts
        location: dict = self.task.cmd.stratagyscan.scan.location
        # 以host的Ip段为主
        if cmdhost is not None and len(cmdhost) > 0:
            ip_ranges = cmdhost
        else:
            self.country_flag = True
            # 国家的二字码查出来就实在是太多了
            ip_ranges = self.get_country_iprange(location)
        # modify by judy 2020/08/06优化的本地IP查询，然后优化了根据国家数据查询ip段过多和去重的问题
        self.split_long_ip_ranges(ip_ranges)
        self._logger.debug("Complete process ip ranges")
        # 这里出来就直接将ipranges的数据存入了self.hosts这里也进行一下置零 by judy 20201202
        ip_ranges = None
        return

    def process_port(self):
        """
        尝试by judy 2020/03/30
        将port加入队列，用于zmap多线程取用
        顺便统计下一共需要扫描的数据 modify by judy 2020/04/08
        :return:
        """
        ports = self.task.cmd.stratagyscan.scan.ports
        self._logger.debug(f"Get input {len(ports)} scan port")
        for p in ports:
            self.port_queue.put(p)
        # 这里统计所有需要扫描的网段计数集合
        self.__all_scan_count = self.port_queue.qsize() * self.__c_ipranges_count
        self._logger.debug(f"There are {self.__all_scan_count} objects will be scan")
        with open("./scan_rate_test_result.txt", "a", encoding="utf-8") as fp:
            fp.write(f"总共有{len(ports)}个端口需要扫描， 总共将有{self.__all_scan_count}个目标需要扫描")
        return

    def split_long_ip_ranges(self, inputipdata):
        """
        拆分长网段1.1.0.0/14 -> 1.1.1.0/24,1.1.2.0/24....
        seres.conn = conn
        seres.res = res
        :return:
        """
        self._logger.debug("Start split long ip ranges to C ip ranges")
        if self.country_flag:
            # 国家数据
            ip_ranges = inputipdata.res
            log = "开始处理地区IP数据"
            self._logger.debug("Get region ip ranges")
            self._outprglog(log)
        else:
            ip_ranges = inputipdata
            log = "开始处理指定IP数据"
            self._logger.debug("Get specific ipranges")
            self._outprglog(log)
        # 存放内网数据
        intranet = []
        for el in ip_ranges:
            # 这里是处理1.1.0.0/24这个网段的
            if isinstance(el, str):
                if (
                    not self.country_flag
                    and IpProcessTools.judge_reserved_ip_addresses(el)
                ):
                    intranet.append(el)
                    self.__c_ipranges_count += 1
                else:
                    try:
                        low_ip = IPy.IP(el)
                        if low_ip.prefixlen() < 24:
                            count = 0
                            for ip in low_ip:
                                if count == 0:
                                    self.__c_ipranges_count += 1
                                    ipstr_list = [str(ip) + "/24"]
                                    self.hosts[tuple(ipstr_list)] = 1
                                count += 1
                                if count == 256:
                                    # 到这里就是一个网段了
                                    count = 0
                        else:
                            self.__c_ipranges_count += 1
                            ipstr_list = [el]
                            self.hosts[tuple(ipstr_list)] = 1
                    except:
                        # 不是ip段，有可能是域名或者其他东西
                        self.__c_ipranges_count += 1
                        ipstr_list = [el]
                        self.host[tuple(ipstr_list)] = 1
            elif isinstance(el, tuple):
                # 这里的数据只能是国家的，所以不需要去重
                # masscan扫描尝试这样
                st = IPy.IP(el[0])
                sp = IPy.IP(el[1])
                # 直接+/24是不准确的，因为很多查出来的数据并没有255个IP
                # 这种数据目前考虑的是单独处理下，但是同时又会带来需要查询的ip过多的问题，先这样做吧，modify by judy 2020/07/22
                if sp.int() - st.int() >= 255:
                    count = 0
                    for i in range(st.int(), sp.int() + 1):
                        if count == 0:
                            self.__c_ipranges_count += 1
                            ipstr: str = IPy.IP(i).strNormal()
                            if ipstr.endswith(".0"):
                                ipstr_list = [(ipstr + "/24")]
                                self.hosts[tuple(ipstr_list)] = 1
                        count += 1
                        if count == 256:
                            count = 0
                else:
                    iptmp = []
                    for i in range(st.int(), sp.int() + 1):
                        o_ipstr: str = IPy.IP(i).strNormal()
                        if o_ipstr.endswith(".0"):
                            continue
                        iptmp.append(o_ipstr)
                    # 这里也表示一个ip段，所以是这个问题才导致了那个进度计算出问题
                    self.__c_ipranges_count += 1
                    self.host[tuple(iptmp)] = 1

            else:
                raise Exception("Unsupported type")

        # 增加内网扫描
        if len(intranet) > 0:
            for oneport in self.task.cmd.stratagyscan.scan.ports:
                with self._file_locker:
                    nmap_scan_host_path = self.tmppath / f"{str(uuid.uuid1())}"
                for shost in intranet:
                    with nmap_scan_host_path.open("a", encoding="utf-8") as fp:
                        fp.write(shost + "\n")
                self.nmap_queue.put((nmap_scan_host_path, [oneport]))

            self._logger.debug(f"本次一共扫描内网{len(intranet)}个目标")

        log = f"一共需要扫描{self.__c_ipranges_count}个IP C段"
        with open("./scan_rate_test_result.txt", "a", encoding="utf-8") as fp:
            fp.write(log + "\n")
        self._logger.info(f"Get {self.__c_ipranges_count} ip ranges")
        self._outprglog(log)
        # 如果是国家查询的ip段，那么需要调用下回调结束函数关闭查询端口
        if self.country_flag:
            inputipdata.sedone()
        self._logger.debug("Complete split long ip ranges to C ip ranges")

    def get_country_iprange(self, countryinfo: dict):
        """
        获取国家二字码的ip段
        在dbip里面选取相关的数据
        :return:
        """
        self._logger.debug("Start get local ip ranges result")
        ip_rangs = None
        try:
            country = countryinfo.get("country")
            province = countryinfo.get("province")
            city = countryinfo.get("city")
            geoid = countryinfo.get("citycode")
            # sa = requests.session()
            # # 开启一个session去拿首页，拿一些访问头和Cookie信息
            # sa.get("http://ipblock.chacuo.net/")
            # # 这里只考虑了C段网络，可能会有A段和B段，后续再加
            # res = sa.get(f'http://ipblock.chacuo.net/down/t_txt=c_{country_code}')
            # res_text = res.text
            # ip_rangs = self.re_iprang.findall(res_text)
            # self._logger.info(f"Start get {country_code} ip range")
            ip_rangs = GeoIPLoc.get_location_ipranges(country, province, city, geoid)
            # 这里是一定能返回一个seres对象的，这里是为了防止sqlite建立了过多的连接，虽然代码没有出过问题
            # 但是为了以防万一还是做了错误处理
            self._logger.info("Complete get local ip ranges result")
        except:
            self._logger.error(
                f"Get country ip rangs error, err:{traceback.format_exc()}"
            )
        return ip_rangs

    def _download_data(self) -> iter:
        """
        下载数据接口，最后返回的数据为dict,
        这里是数据下载流程的开始，新增暂停功能
        modify by judy 2020/06/03
        :return:
        """
        # 为了计算进度，一定是先处理host再处理port
        self.process_host()
        self.process_port()
        # 不间断的获取停止标识
        # t = threading.Thread(target=self._get_stop_sign, name="stop_singn_scan")
        # t.start()
        # 1、zmap快速发现开放端口
        # 想要开线程这里就得放在队列里,搞成一共在运行的线程
        mzsq = threading.Thread(
            target=self.make_zmap_scan_queue, name="make_zmap_scan_queue"
        )
        mzsq.start()
        for i in range(max_zscan_threads):
            t = threading.Thread(target=self.zmap_scan, name=f"zmap_threads{i}")
            t.start()
        for j in range(max_nscan_threads):
            jthread = threading.Thread(target=self.nmap_scan, name=f"scan_threads{j}")
            jthread.start()

        for m in range(max_zgrab2_threads):
            mthread = threading.Thread(
                target=self.zgrab2_scan, name=f"zgrab2_threads{m}"
            )
            mthread.start()

        for n in range(max_vulns_threads):
            nthreads = threading.Thread(
                target=self.vulns_scan, name=f"vulns_threads{n}"
            )
            nthreads.start()
        # t = threading.Thread(target=self._scan_status, name="Monitor scan status")
        # t.start()
        ossq = threading.Thread(target=self.output_res, name=f"output_result")
        ossq.start()
        ossq.join()
        # 程序执行完成
        self._running = False
        # 扫描完成应该给一个100%
        self._logger.info("All scan complete")
        self.task.progress = 1
        self._write_iscantaskback(ECommandStatus.Dealing, "扫描完成：100%")
        log = f"此次IP刺探任务已完成，总共刺探到了{self.output_count}条数据"
        self._outprglog(log)
        return
        yield None

    def make_zmap_scan_queue(self):
        """
        生成zmap扫描的队列，主要是为了保证速率
        modify by judy 2020/06/03
        如果停止了那么也就不继续制作zmap扫描数据了
        port :
        host :list []
        :return:
        """
        self._logger.info("Start make zmap scan data and insert to zmap queue")
        self.make_zmap_scan_queue_status = True
        save_port_count = None
        save_host_count = None
        if self.sp.exists():
            save_str = self.sp.read_text()
            if save_str is not None and save_str != "":
                save_list = save_str.split(" ")
                save_port_count = int(save_list[0])
                save_host_count = int(save_list[1])
        # 这种中继数据只使用一次
        host_count = 0
        port_count = 0
        got = False
        while True:
            if self.port_queue.empty() or self._stop_sign:
                # 运行结束
                self.make_zmap_scan_queue_status = False
                self._logger.info("Complete make zmap scan data")
                break
            got = False
            port = self.port_queue.get()
            port_count += 1
            got = True
            if save_port_count is not None and save_port_count > port_count:
                continue
            elif save_port_count == port_count:
                # 找到了当前续传的port就将这个数据删除了
                self._logger.info(
                    f"Continue download, skip {save_port_count} port, start from port:{port.port}"
                )
                save_port_count = None
                pass
            tmp_hosts = []
            try:
                # C段的ip
                for host in self.hosts.keys():
                    host_count += 1
                    if (
                        save_host_count is not None
                        and save_host_count > 0
                        and save_host_count > host_count
                    ):
                        continue
                    elif save_host_count == host_count:
                        self._logger.info(
                            f"Continue download, skip {save_host_count} host, start from host:{host}"
                        )
                        save_host_count = None
                        pass
                    # 记录目前扫描到了哪个host
                    line = f"{port_count} {host_count-self.zmap_queue.qsize()}"
                    self.sp.write_text(line)
                    # self._logger.info(f"Write line:{line}")
                    # 唯一元组转换成列表
                    host = list(host)
                    tmp_hosts.extend(host)
                    if len(tmp_hosts) > max_zscan_ipranges:
                        while self.zmap_queue.qsize() > max_zscan_threads * 10:
                            self._logger.debug(
                                f"Zmap scan queue over {max_zscan_threads*10}, too many objects to scan, wait 10 second"
                            )
                            time.sleep(10)
                        self.zmap_queue.put((tmp_hosts, port))
                        # 复原tmp_host
                        tmp_hosts = []
                # 单个的IP或者是host
                for ip in self.host.keys():
                    host_count += 1
                    if (
                        save_host_count is not None
                        and save_host_count > 0
                        and save_host_count > host_count
                    ):
                        continue
                    elif save_host_count == host_count:
                        self._logger.info(
                            f"Continue download, skip {save_host_count} host, start from host:{ip}"
                        )
                        save_host_count = None
                        pass
                    # 记录目前扫描到了哪个host
                    line = f"{port_count} {host_count-self.zmap_queue.qsize()}"
                    self.sp.write_text(line)
                    host = list(ip)
                    tmp_hosts.extend(host)
                    if len(tmp_hosts) > max_zscan_ip:
                        while self.zmap_queue.qsize() > max_zscan_threads * 10:
                            self._logger.debug(
                                f"Zmap scan queue over {max_zscan_threads*10}, too many objects to scan, wait 10 second"
                            )
                            time.sleep(10)
                        self.zmap_queue.put((tmp_hosts, port))
                        # 复原tmp_host
                        tmp_hosts = []
            except:
                self._logger.error(
                    f"Put hosts port to zmap scan queue error\nport:{port.port}\nerror:{traceback.format_exc()}"
                )
            finally:
                # 最后执行完成查看下该端口下还有没有剩余的hosts
                if len(tmp_hosts) > 0:
                    self.zmap_queue.put((tmp_hosts, port))
                if got:
                    self.port_queue.task_done()
        # 当前函数执行完成后手动释放下dict,by swm 20201012
        self.hosts = None
        self.host = None

    def _make_back_progress(self):
        """
        扫描进度的回馈
        这个扫描进度，不应该考虑到扫描整个国家的情况
        那就直接在imap里面算，
        但是这个东西是并行的怎么算
        唉，先写着用用吧，by Judy 2020/04/07
        :return:
        """
        try:
            progress = round(self.__has_scan_count / self.__all_scan_count, 2)
            if progress - self.__progress > 0.001:
                # 这里的进度估算并不准确，有时会超过1
                if progress > 1.0:
                    progress = 0.999
                self.__progress = progress
                self.task.progress = progress
                self._logger.info(f"Scan progress:{float(progress * 100)}%")
                self._outprglog(f"正在扫描:{float(progress * 100)}%")
                self._write_iscantaskback(
                    ECommandStatus.Dealing, f"正在扫描:{float(progress * 100)}%"
                )
        except:
            self._logger.error(f"Make progress error, err:{traceback.format_exc()}")
        return

    def _is_complete(self, scan_queue, scan_thread_state) -> bool:
        """
        这里判断任务是否完成,程序全部执行完成了返回true
        任务没有执行完成返回false
        modify by judy 2020/06/03
        如果任务被中途暂停了那么直接停止
        :return:
        """
        if self._stop_sign:
            return True

        complete = False
        # 0表示线程还没有开始
        if len(scan_thread_state) == 0:
            return complete

        if scan_queue.empty() and True not in scan_thread_state.values():
            # 队列为空，并且已经没有任务正在执行中了
            complete = True
        return complete

    def zmap_scan(self):
        """
        使用zmap来发现存活的端口
        zmap和masscan的效果类似，现在尝试使用masscan来扫描
        换了试试
        :return:
        """
        # 当前线程的唯一标识，进来以后扫描就开始
        ident = threading.current_thread().ident
        cur_state = True
        self.__zmap_scan_thread_state[ident] = cur_state
        got = False
        while True:
            # 运行结束
            if (
                not self.make_zmap_scan_queue_status and self.zmap_queue.empty()
            ) or self._stop_sign:
                # 所有的端口已经扫描完成
                cur_state = False
                self.__zmap_scan_thread_state[ident] = cur_state
                self._logger.info(f"Zmap {ident} Scan complete")
                break

            if self.zmap_queue.empty():
                time.sleep(1)
                continue
            got = False
            hosts, t_port = self.zmap_queue.get()
            got = True
            # 这里去判断下， 如果是domain的话
            new_host, ip_domain_dict = IpProcessTools.judge_ip_or_domain(hosts)
            if len(new_host) == 0:
                self._logger.debug(f"Get no live hosts")
                continue
            log = f"开始探测{len(hosts)}个主机存活和端口开放情况, PORT:{t_port.port}, Protocol:{t_port.flag}"
            self._outprglog(log)
            # zmap每次扫描的很快，所以可以多给点ip段，但是命令行装不了那么多，所以以文件的方式传参注意创建文件和删除文件，by judy 2020/08/20
            with self._file_locker:
                zmap_scan_host_path = self.tmppath / f"{str(uuid.uuid1())}"
            for shost in new_host:
                with zmap_scan_host_path.open("a", encoding="utf-8") as fp:
                    fp.write(shost + "\n")
            self._logger.debug(
                f"Start Zmap thread scan an object, zmap thread id: {ident}"
            )
            try:
                for port_info in self.zmap.scan_open_ports(
                    self.task, 1, zmap_scan_host_path, [t_port]
                ):
                    if port_info is None:
                        continue
                    ip = port_info._host
                    o_host = ip_domain_dict.get(ip)
                    if o_host is not None:
                        zmapres = o_host
                    else:
                        zmapres = ip
                    with self._nmap_tmp_dict_locker:
                        self.process_nmap_data(zmapres, t_port)
                self.__has_scan_count += 1
            except:
                self._logger.error(
                    f"Zmap scan port error\nport:{t_port.port} protocol:{t_port.flag}\nerror:{traceback.format_exc()}"
                )
            finally:
                if got:
                    self.zmap_queue.task_done()
                if zmap_scan_host_path.exists():
                    zmap_scan_host_path.unlink()
                self._logger.debug(f"Zmap {ident} complete scan an object")
        # 退出break后
        if True not in self.__zmap_scan_thread_state.values():
            with self._nmap_tmp_dict_locker:
                self.process_nmap_data(None, None, True)

    def process_nmap_data(self, ip, port, zscan_stop_flasg=False):
        """
        处理nmap的数据
        将nmap累加到一定的数量后再放到nmap扫描
        这样nmap的速率会比较均衡
        """
        if ip is not None and port is not None:
            if self._nmap_tmp.__contains__(port):
                self._nmap_tmp[port].append(ip)
                if len(self._nmap_tmp.get(port)) >= max_nmap_ip:
                    with self._file_locker:
                        nmap_scan_path = self.tmppath / f"{str(uuid.uuid1())}"
                    with nmap_scan_path.open("a", encoding="utf-8") as fp:
                        fp.writelines([ip + "\n" for ip in self._nmap_tmp.get(port)])

                    # 队列不宜堆积过多
                    while self.nmap_queue.qsize() > max_nscan_threads * 10:
                        self._logger.debug(
                            f"Nmap scan queue over {max_nscan_threads*10}, too many objects to scan, wait 20 seconds"
                        )
                        time.sleep(10)
                    self.nmap_queue.put((nmap_scan_path, port))
                    # 入队后出栈
                    self._nmap_tmp.pop(port)
            else:
                self._nmap_tmp[port] = [ip]
        # 判断一下zmap是否结束了
        if zscan_stop_flasg:
            for port, ipranges in self._nmap_tmp.items():
                with self._file_locker:
                    nmap_scan_path = self.tmppath / f"{str(uuid.uuid1())}"
                with nmap_scan_path.open("a", encoding="utf-8") as fp:
                    fp.writelines([ip + "\n" for ip in ipranges])
                self.nmap_queue.put((nmap_scan_path, port))
            # 将所有的nmap加入队列后就将这个数据清空
            self._nmap_tmp = {}
        return

    def nmap_scan(self):
        """
        nmap
        这里会使用多线程去处理已经查到开放了的端口
        :return:
        """
        ident = threading.current_thread().ident
        cur_state = True
        self.__nmap_scan_thread_state[ident] = cur_state
        got = False
        while True:
            # 扫描完成退出
            if self._is_complete(self.nmap_queue, self.__zmap_scan_thread_state):
                cur_state = False
                self.__nmap_scan_thread_state[ident] = cur_state
                self._logger.info(f"Nmap {ident} scan complete")
                break
            # 查看下队列里面还有没有东西
            if self.nmap_queue.empty():
                time.sleep(1)
                continue
            got = False
            ips_path, port = self.nmap_queue.get()
            got = True
            log = f"开始探测主机协议: PORT:{port.port} protocol:{port.flag}"
            self._logger.info(
                f"Start nmap {ips_path.as_posix()}, port:{port.port} protocol:{port.flag}"
            )
            self._outprglog(log)
            try:
                tmp_zgrab2_dict = {}
                self._logger.debug(f"Start Nmap scan an object, nmap thread id:{ident}")
                for portinfo in self.nmap.scan_open_ports_by_file(
                    self.task, 1, ips_path.as_posix(), [port], outlog=self._outprglog
                ):
                    if not isinstance(portinfo, PortInfo):
                        continue
                    # 这里出来的全是一个端口下的东西，尼玛的还好这里去重了
                    tmp_zgrab2_dict[portinfo._host] = portinfo

                if len(tmp_zgrab2_dict) > 0:
                    while self.zgrab2_queue.qsize() > max_zgrab2_threads * 10:
                        # 一分钟去检测一次队列的处理情况
                        self._logger.debug(
                            f"Nmap Threading id:{ident},Zgrab2 scan queue over {max_zgrab2_threads*10}, too many objects to scan, wait 20 second"
                        )
                        time.sleep(20)
                    self.zgrab2_queue.put((tmp_zgrab2_dict, port))
                    log = f"探测主机协议完成：{list(tmp_zgrab2_dict.keys())}"
                    self._logger.info(
                        f"Get nmap result {tmp_zgrab2_dict.__len__()} ips and put into zgrab2"
                    )
                    self._outprglog(log)
            except:
                self._logger.error(
                    f"Nmap scan port info error, id:{ident}, err:{traceback.format_exc()}"
                )
            finally:
                if got:
                    self.nmap_queue.task_done()
                    # 回馈进度
                    self._make_back_progress()
                    # 删除文件
                    try:
                        if ips_path.exists():
                            ips_path.unlink()
                    except:
                        self._logger.error(
                            f"Delete zmap res path error, err:{traceback.format_exc()}"
                        )
                    self._logger.debug(
                        f"Complete Nmap scan an object, nmap thread id:{ident}"
                    )

    def zgrab2_scan(self):
        """
        zgrab2扫描
        port:PortInfo
        :return:
        """
        # 当前线程的唯一标识，进来以后扫描就开始
        ident = threading.current_thread().ident
        cur_state = True
        self.__zgrab2_scan_thread_state[ident] = cur_state
        got = False
        while True:
            # 运行结束
            if self._is_complete(self.zgrab2_queue, self.__nmap_scan_thread_state):
                cur_state = False
                self.__zgrab2_scan_thread_state[ident] = cur_state
                self._logger.info(f"Zgrab2 {ident} scan complete")
                break
            if self.zgrab2_queue.empty():
                time.sleep(0.1)
                continue
            got = False
            portinfo_dict, port = self.zgrab2_queue.get()
            got = True
            log = f"开始协议详情探测：{list(portinfo_dict.keys())}"
            self._logger.info(
                f"Start zgrab2 scan {len(portinfo_dict)} ips, zgrab2 thread id:{ident}"
            )
            self._outprglog(log)
            try:
                self._scan_application_protocol(1, portinfo_dict, port)
                self._logger.debug(f"Complete Zgrab2 scan, zgrab2 thread id:{ident}")
                for portinfo in portinfo_dict.values():
                    while self.vulns_queue.qsize() > max_vulns_threads * 10:
                        self._logger.debug(
                            f"Zgrab2 threading id:{ident},Vulns scan queue over {max_vulns_threads*10} objects, too many data, wait 20 second"
                        )
                        time.sleep(20)
                    self.vulns_queue.put(portinfo)
                log = f"协议详情探测： 获取到{len(portinfo_dict)}个结果"
                self._logger.info(f"Put {len(portinfo_dict)} objects to vuls queue")
                self._outprglog(log)
            except Exception as err:
                self._logger.error(f"Zgrab2 scans error, err:{err}")
            finally:
                # 手动释放下dict对象
                if got:
                    portinfo_dict = None
                    self.zgrab2_queue.task_done()

    def _scan_application_protocol(self, level: int, port_info_dict, port):
        """
        根据 portinfo 的协议类型，扫描其应用层协议
        增加效率，每次扫描一个网段的数据，不再去扫描一个单一的那样太慢了
        """
        try:
            # 这个ports是直接关联的最开始处理的那个port,所以直接取第一个,modify by judy
            port = port.port
            # 先去扫一遍：
            if port != 80:
                self.zgrab2.get_tlsinfo(
                    self.task, level, port_info_dict, port, outlog=self._outprglog
                )
            tmpdict = {}
            # 没有协议的端口
            portdict = {}
            # 进行协议分类
            for k, v in port_info_dict.items():
                # k是ip, v是portinfo
                service = v.service
                if service is not None:
                    ser_dict = tmpdict.get(service)
                    # 判断里面有没有该类型的dict
                    if ser_dict is None:
                        tmpdict[service] = {}
                    # 添加
                    tmpdict[service][k] = v
                else:
                    # 没有协议只有端口的字典
                    portdict[k] = v
            # 扫描只有端口的应用层协议
            self._scan_port_application(level, portdict, port)
            # 拿到每一类的东西去扫描
            for service, service_dict in tmpdict.items():
                if service == "ftp":
                    self.zgrab2.get_ftp_info(
                        self.task, level, service_dict, port, outlog=self._outprglog
                    )
                elif service == "ssh":
                    self.zgrab2.get_ssh_info(
                        self.task, level, service_dict, port, outlog=self._outprglog
                    )
                elif service == "telnet":
                    self.zgrab2.get_telnet_info(
                        self.task, level, service_dict, port, outlog=self._outprglog
                    )
                elif service == "smtp":
                    self.zgrab2.get_smtp_info(
                        self.task, level, service_dict, port, outlog=self._outprglog
                    )
                elif service.__contains__("http") or service.__contains__("tcpwrapped"):
                    self.zgrab2.get_siteinfo(
                        self.task, level, service_dict, port, outlog=self._outprglog
                    )
                elif service == "pop3":
                    self.zgrab2.get_pop3_info(
                        self.task, level, service_dict, port, outlog=self._outprglog
                    )
                elif service == "ntp":
                    self.zgrab2.get_ntp_info(
                        self.task, level, service_dict, port, outlog=self._outprglog
                    )
                elif service == "imap":
                    self.zgrab2.get_imap_info(
                        self.task, level, service_dict, port, outlog=self._outprglog
                    )
                elif service == "mssql":
                    self.zgrab2.get_mssql_info(
                        self.task, level, service_dict, port, outlog=self._outprglog
                    )
                elif service == "redis":
                    self.zgrab2.get_redis_info(
                        self.task, level, service_dict, port, outlog=self._outprglog
                    )
                elif service == "mongodb":
                    self.zgrab2.get_mongodb_info(
                        self.task, level, service_dict, port, outlog=self._outprglog
                    )
                elif service == "mysql":
                    self.zgrab2.get_mysql_info(
                        self.task, level, service_dict, port, outlog=self._outprglog
                    )
                elif service == "oracle":
                    self.zgrab2.get_oracle_info(
                        self.task, level, service_dict, port, outlog=self._outprglog
                    )
                else:
                    # 协议不在这里面的
                    self._scan_port_application(level, service_dict, port)
        except:
            self._logger.error(
                "Scan ip port application protocol error:\ntaskid:{}\nerror:{}".format(
                    self.task.taskid, traceback.format_exc()
                )
            )

    def _scan_port_application(self, level, portdict, port):
        """
        这个主要是为了扫描一些没有协议的端口，或者协议没在上面那个方法里的
        :return:
        """
        if len(portdict) == 0:
            return
        if port == 21:
            self.zgrab2.get_ftp_info(self.task, level, portdict, port)
        elif port == 22:
            self.zgrab2.get_ssh_info(self.task, level, portdict, port)
        elif port == 23:
            self.zgrab2.get_telnet_info(self.task, level, portdict, port)
        elif port == 25 or port == 465:
            self.zgrab2.get_smtp_info(self.task, level, portdict, port)
        elif port == 80 or port == 443:
            self.zgrab2.get_siteinfo(self.task, level, portdict, port)
        elif port == 110 or port == 995:
            self.zgrab2.get_pop3_info(self.task, level, portdict, port)
        elif port == 123:
            self.zgrab2.get_ntp_info(self.task, level, portdict, port)
        elif port == 143 or port == 993:
            self.zgrab2.get_imap_info(self.task, level, portdict, port)
        elif port == 1433:
            self.zgrab2.get_mssql_info(self.task, level, portdict, port)
        elif port == 6379:
            self.zgrab2.get_redis_info(self.task, level, portdict, port)
        elif port == 27017:
            self.zgrab2.get_mongodb_info(self.task, level, portdict, port)
        elif port == 3306:
            self.zgrab2.get_mysql_info(self.task, level, portdict, port)
        elif port == 1521:
            self.zgrab2.get_oracle_info(self.task, level, portdict, port)

    def vulns_scan(self):
        """
        漏洞扫描，回去扫描某个ip的具体页面
        由于是http连接非常耗性能，因此页面做了勾选和筛选
        """
        # 当前线程的唯一标识，进来以后扫描就开始
        ident = threading.current_thread().ident
        cur_state = True
        self.__vulns_scan_thread_state[ident] = cur_state
        got = False
        while True:
            # 运行结束
            if self._is_complete(self.vulns_queue, self.__zgrab2_scan_thread_state):
                cur_state = False
                self.__vulns_scan_thread_state[ident] = cur_state
                self._logger.info(f"Vulns {ident} scan complete")
                break
            if self.vulns_queue.empty():
                time.sleep(0.1)
                continue
            got = False
            portinfo = self.vulns_queue.get()
            got = True
            if len(self._vulns_list) > 0:
                log = f"开始漏洞扫描： {self._vulns_list}"
                self._logger.debug(
                    f"Start vulns scan {self._vulns_list}, vulns threading id:{ident}"
                )
                self._outprglog(log)
                self.logicalgrabber.grabbanner(
                    portinfo, self._vulns_list, flag="iscan", outlog=self._outprglog
                )
            try:
                self.output_res_queue.put(portinfo)
            except Exception as err:
                self._logger.error(f"vulns scan error, err:{err}")
            finally:
                if got:
                    self.vulns_queue.task_done()
                    if len(self._vulns_list) > 0:
                        self._logger.debug(
                            f"Stop vulns scan {self._vulns_list}, vulns threading id:{ident}"
                        )

    def output_res(self):
        """
        结果输出线程
        :return:
        """
        self._logger.info(f"Start output result thread")
        got = False
        while True:
            # 结束
            if self._is_complete(self.output_res_queue, self.__vulns_scan_thread_state):
                self._logger.info(f"Complete output result thread")
                # 正常结束就删除当前任务的扫描进度文件
                self.sp.unlink()
                break

            if self.output_res_queue.empty():
                time.sleep(0.1)
                continue
            try:
                got = False
                portinfo: PortInfo = self.output_res_queue.get()
                got = True
                file_port = portinfo._port
                ip = portinfo._host
                root: IP = IP(self.task, 1, ip)
                root.set_portinfo(portinfo)
                geoinfo, org, isp = self.dbip.get_ip_mmdbinfo(level=1, ip=ip)
                country_code = "unknown"
                if isinstance(geoinfo, GeoInfo):
                    root.set_geolocation(geoinfo)
                    country_code = geoinfo._country_code
                root.org = org
                root.isp = isp
                if root._subitem_count() > 0:
                    out_dict = root.get_outputdict()
                    # 输出锁，防止输出互锁，好像原本输出里面就有输出锁，先测试不用锁的情况看会不会出问题
                    # with self._file_locker:
                    file_name = f"{country_code}_{file_port}_{int(time.time() * 1000)}"
                    self._outputdata(out_dict, file_name=file_name)
                    self.output_count += 1
                if isinstance(portinfo, PortInfo):
                    del portinfo
            except:
                self._logger.error(f"Output result error: {traceback.format_exc()}")
            finally:
                if got:
                    self.output_res_queue.task_done()
