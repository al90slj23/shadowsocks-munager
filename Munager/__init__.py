import logging

from tornado import gen
from tornado.httpclient import AsyncHTTPClient, HTTPError
from tornado.ioloop import IOLoop, PeriodicCallback

from Munager.MuAPI import MuAPI
from Munager.V2Manager import V2Manager
from Munager.SpeedTestManager import speedtest_thread
import json


class Munager:
    def __init__(self, config):
        self.config = config

        # set logger
        self.logger = logging.getLogger()

        # mix
        self.ioloop = IOLoop.current()
        self.mu_api = MuAPI(self.config)
        self.node_info = self.mu_api.get_node_info()
        self.logger.info("Node infos: {}".format(self.node_info))

        self.manager = V2Manager(self.config, self.node_info)
        self.logger.info('Munager initializing.')

        self.first_time_start = True

    @gen.coroutine
    def upload_serverload(self):
        # update online users count
        try:
            uptime = self._uptime()
            load = self._load()
            result = yield self.mu_api.upload_systemload(uptime, load)
            if result:
                self.logger.info('upload_system load success. uptime {}, load {}'.format(uptime, load))
        except HTTPError:
            self.logger.warning('upload_system load failed')

    @gen.coroutine
    def upload_speedtest(self):
        # update online users count
        try:
            speedtest_result = speedtest_thread()
            result = yield self.mu_api.upload_speedtest(speedtest_result)
            if result:
                self.logger.info('Successfully upload speet test result {}.'.format(speedtest_result))
        except HTTPError:
            self.logger.warning('failed to upload online user count.')

    @gen.coroutine
    def update_manager(self):
        new_node_info = self.mu_api.get_node_info()
        if self.node_info != new_node_info:
            self.node_info = new_node_info
            self.manager.if_user_change = True
            self.manager.update_node_info(self.node_info)
        # get from MuAPI and ss-manager
        users = yield self.mu_api.get_users('email', self.node_info)
        current_user = self.manager.get_users()
        self.logger.info('get MuAPI and ss-manager succeed, now begin to check ports.')
        # self.logger.debug('get state from ss-manager: {}.'.format(state))

        # remove user by prefixed_id
        for prefixed_id in current_user:
            if prefixed_id not in users or not users.get(prefixed_id).available:
                self.manager.remove(prefixed_id)
                self.logger.info('remove client: {}.'.format(prefixed_id))

        # add prefixed_id
        for prefixed_id, user in users.items():
            if user.available and prefixed_id not in current_user:
                if self.manager.add(user):
                    self.logger.info('add user email {}.'.format(prefixed_id))

            if user.available and prefixed_id in current_user:
                if user != current_user.get(prefixed_id):
                    if self.manager.remove(prefixed_id) and self.manager.add(user):
                        self.logger.info('reset user {} due to method or password changed.'.format(prefixed_id))

        # check finish
        self.logger.info('check ports finished.')
        self.logger.info("if update {}".format(self.manager.if_user_change))
        if self.manager.if_user_change or self.first_time_start:
            self.manager.update_config()
            self.manager.loader.write()
            self.manager.if_user_change = False
            self.first_time_start = False
            self.manager.loader.restart()

    @gen.coroutine
    def upload_throughput(self):
        current_user = self.manager.get_users()
        online_amount = 0
        for prefixed, user in current_user.items():
            laset_traffic_upload,laset_traffic_download,user_id= self.manager.get_last_traffic(user)
            current_upload,current_download = user.get_throughput()
            if current_download+current_upload < laset_traffic_upload+laset_traffic_download:
                online_amount += 1
                self.logger.warning('error throughput, try fix.')
                self.manager.set_current_traffic(user, upload=current_upload,download=current_download)
            elif current_download+current_upload > laset_traffic_upload+laset_traffic_download:
                online_amount += 1
                upload_dif = current_upload - laset_traffic_upload
                download_dif = current_download - laset_traffic_download
                try:
                    result = yield self.mu_api.upload_throughput(user_id, upload=upload_dif,donwload=download_dif)
                    if result:
                        self.manager.set_current_traffic(user=user,upload=current_upload,download=current_download)
                        self.logger.info('update traffic: Upload {}, Download {} for user: {}.'.format(upload_dif,
                                                                                                       download_dif,
                                                                                                       user.prefixed_id))
                except:
                    self.logger.info('update trafic faileds')

        # update online users count
        try:
            result = yield self.mu_api.post_online_user(online_amount)
            if result:
                self.logger.info('upload online user count: {}.'.format(online_amount))
        except HTTPError:
            self.logger.warning('failed to upload online user count.')

    @staticmethod
    def _second_to_msecond(period):
        # s to ms
        return period * 1000

    @staticmethod
    def _uptime():
        with open('/proc/uptime', 'r') as f:
            return float(f.readline().split()[0])

    @staticmethod
    def _load():
        import os
        return os.popen(
            "cat /proc/loadavg | awk '{ print $1\" \"$2\" \"$3 }'").readlines()[0]

    def run(self):
        # period task
        PeriodicCallback(
            callback=self.update_manager,
            callback_time=self._second_to_msecond(self.config.get('update_port_period', 60)),
            io_loop=self.ioloop,
        ).start()
        PeriodicCallback(
            callback=self.upload_throughput,
            callback_time=self._second_to_msecond(self.config.get('upload_throughput_period', 360)),
            io_loop=self.ioloop,
        ).start()
        PeriodicCallback(
            callback=self.upload_serverload,
            callback_time=self._second_to_msecond(self.config.get("upload_serverload_period", 60)),
            io_loop=self.ioloop,
        ).start()
        PeriodicCallback(
            callback_time=self._second_to_msecond(self.config.get("upload_speedtest_period",21600)),
            callback=self.upload_speedtest,
            io_loop=self.ioloop
        ).start()
        try:
            # Init task
            self.ioloop.run_sync(self.update_manager)
            self.ioloop.start()
        except KeyboardInterrupt:
            del self.mu_api
            del self.manager
            print('Bye~')
