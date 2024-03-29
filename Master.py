from xmlrpc.client import ServerProxy
from xmlrpc.server import SimpleXMLRPCServer

import os.path
import pickle
import random
import threading
import time
import sys

import py_eureka_client.eureka_client as eureka_client


class FileInfo():
    def __init__(self, deleted=False, time=None, chunk_list=[]):
        self.deleted = deleted
        self.deleted_time = time # float deletion time
        self.chunk_list = chunk_list # list[(int,int)] chunkIds and versionNumbers

class Master():
    def __init__(self, n_replicas):
        random.seed(0)
        self.num_replicas = n_replicas
        self.deleted_file_duration_secs = 30

        self.chunkserver_url_to_proxy = {}
        self.filename_to_chunks = {} # string filename -> FileInfo
        self.chunk_to_filename = {} # int chunkId -> string filename
        self.chunk_to_urls = {} # int chunkId -> list[string] urls of replicas
        self.chunk_id_counter = 1000 # start at 1000 to differentiate from chunk indexes

        self.root_dir = '/home/mohamed/temp/master/'
        self.init_from_log()
        self.thread_interval = 30
        self.url_to_heartbeat_time = {} # string url -> float time of last hearbeat
        background_thread = threading.Thread(target=self.background_thread, \
                args=[self.thread_interval])
        background_thread.daemon = True
        background_thread.start()
        print('master initialized')
        your_rest_server_port = 9000
        eureka_client.init(eureka_server="http://10.172.0.2:8761",
                                app_name="Master",
                                instance_port=your_rest_server_port, instance_id="1")
        print('master registered')

    # init_from_log:
    # Initializes the master from its log, if it exists. Used to recover in-memory metadata
    # after a master crash. It recovers its chunk to replica mapping by asking all
    # chunkservers for what chunks they own.
    def init_from_log(self):
        if not os.path.isfile(self.root_dir + 'log.txt'):
            print('initializing from scratch')
            return
        print('initializing from log')
        chunkserver_urls = []

        # read persistent data from log
        with open(self.root_dir + 'log.txt', 'rb') as f:
            chunkserver_urls = pickle.load(f)
            self.filename_to_chunks = pickle.load(f)
            self.chunk_id_counter = pickle.load(f)

        # initialize self.chunk_id_to_filename to easily check mapping
        for filename in self.filename_to_chunks:
            chunk_list = self.filename_to_chunks[filename].chunk_list
            for chunk_id, version in chunk_list:
                self.chunk_to_filename[chunk_id] = filename

        # get chunks from chunkservers in order to create chunk to replicas mapping
        for url in chunkserver_urls:
            cs_proxy = ServerProxy(url)
            try:
                chunks_on_chunkserver = cs_proxy.get_chunks()
            except:
                # chunkserver is down
                continue
            self.chunkserver_url_to_proxy[url] = cs_proxy
            for chunk_id, version in chunks_on_chunkserver:
                if chunk_id not in self.chunk_to_urls:
                    self.chunk_to_urls[chunk_id] = []
                self.chunk_to_urls[chunk_id].append(url)

    # flush_to_log:
    # Helper function to flush necessary in-memory data to disk.
    def flush_to_log(self):
        with open(self.root_dir + 'log.txt', 'wb') as f:
            pickle.dump(list(self.chunkserver_url_to_proxy.keys()), f)
            pickle.dump(self.filename_to_chunks, f)
            pickle.dump(self.chunk_id_counter, f)
        print('flushed metadata to log')
    
    # background_thread:
    # Thread that continuously runs to handle various background tasks.
    def background_thread(self, interval):
        while True:
            self.garbage_collect()
            self.check_heartbeats()
            self.rereplicate_chunks()
            time.sleep(interval)

    # check_heartbeats:
    # Runs periodically in the background thread to check what chunkservers have not sent
    # heartbeats recently. If a chunkserver has not sent a heartbeat, we assume it is down
    # and remove it from our metadata.
    def check_heartbeats(self):
        urls_to_delete = []
        for url in self.url_to_heartbeat_time:
            last_heartbeat_time = self.url_to_heartbeat_time[url]
            # set expiration of heartbeat to be thread interval + delta
            heartbeat_expiration = last_heartbeat_time + self.thread_interval + 5
            if time.time() > heartbeat_expiration:
                print('did not receive heartbeat from', url)
                self.remove_chunkserver(url)
                urls_to_delete.append(url)
        for url in urls_to_delete:
            del self.url_to_heartbeat_time[url]

    # heartbeat:
    # Collects heartbeats from a chunkserver and notes the time the heartbeat was received.
    # The server also sends a list of chunks it owns, and the master will respond with
    # the chunks that it no longer has metadata for. The chunkserver will then delete these
    # chunks.
    def heartbeat(self, url, chunk_ids):
        print('received heartbeat from', url)
        self.url_to_heartbeat_time[url] = time.time()

        # if we're hearing again from a server we thought was down:
        if url not in self.chunkserver_url_to_proxy:
            self.chunkserver_url_to_proxy[url] = ServerProxy(url)
            # add this replica url back into chunk to url mapping
            deleted_chunk_ids = self.link_with_master(url, chunk_ids)
            if len(deleted_chunk_ids) > 0:
                print('does not have metadata for:', deleted_chunk_ids)
            return deleted_chunk_ids

        # otherwise, just scan for deleted chunk ids
        deleted_chunk_ids = []
        for chunk_id, version in chunk_ids:
            if chunk_id not in self.chunk_to_filename:
                deleted_chunk_ids.append(chunk_id)
        if len(deleted_chunk_ids) > 0:
            print('does not have metadata for:', deleted_chunk_ids)
        return deleted_chunk_ids

    # garbage_collect:
    # Runs periodically in the background to check all filenames that have been deleted.
    # If the file has been deleted (renamed to DELETED_original_filename) and the deletion
    # wait time has passed, we delete its metadata.
    def garbage_collect(self):
        #iterate through file namespace and remove old deleted files
        filenames_to_delete = []
        for filename in self.filename_to_chunks:
            f = self.filename_to_chunks[filename]
            if f.deleted:
                if time.time() > f.deleted_time + self.deleted_file_duration_secs:
                    filenames_to_delete.append(filename)
        if len(filenames_to_delete) > 0:
            print('deleting filenames:', filenames_to_delete)
        for filename in filenames_to_delete:
            del self.filename_to_chunks[filename]

        #iterate through chunk namespace and remove orphaned chunks
        chunks_to_delete = []
        for chunkId in self.chunk_to_filename:
            f = self.chunk_to_filename[chunkId]
            deleted_f = 'DELETED_' + f
            if f not in self.filename_to_chunks and \
                    deleted_f not in self.filename_to_chunks:
                chunks_to_delete.append(chunkId)
        if len(chunks_to_delete) > 0:
            print('deleting chunks:', chunks_to_delete)
        for chunkId in chunks_to_delete:
            del self.chunk_to_filename[chunkId]
            del self.chunk_to_urls[chunkId]

        if len(filenames_to_delete) > 0 or len(chunks_to_delete) > 0:
            self.flush_to_log()
        print('after garbage collection:', self.filename_to_chunks, self.chunk_to_filename)

    # link_with_master:
    # A chunkserver calls this when it starts to link with the master. The master will then
    # add this chunkserver and its chunks to its own metadata. Since this will be called when
    # a replica recovers after crashing, it also checks versions in the chunk list for any
    # stale chunks, and reports them if found.
    def link_with_master(self, url, chunk_list):
        self.url_to_heartbeat_time[url] = time.time()
        self.chunkserver_url_to_proxy[url] = ServerProxy(url)
        stale_chunks = []
        for chunk_id, version in chunk_list:
            if chunk_id not in self.chunk_to_filename:
                stale_chunks.append(chunk_id)
                continue
            cur_version, chunk_idx = self.lookup_chunk_version(chunk_id)
            if cur_version == None:
                return 'chunk id not found in master metadata'
            # check if version number is stale. if it is, return list of stale chunks
            if version < cur_version:
                # stale replica
                stale_chunks.append(chunk_id)
                # don't add this url to list of replicas for chunk
                continue
            if version > cur_version:
                # master is out of date, so we take the newer version
                filename = self.chunk_to_filename[chunk_id]
                self.filename_to_chunks[filename].chunk_list[chunk_idx] = (chunk_id, version)
            if chunk_id not in self.chunk_to_urls:
                self.chunk_to_urls[chunk_id] = []
            if url not in self.chunk_to_urls[chunk_id]:
                self.chunk_to_urls[chunk_id].append(url)
        self.flush_to_log()
        print('chunkserver_url_to_proxy:', self.chunkserver_url_to_proxy)
        return stale_chunks

    # remove_chunkserver:
    # Removes the specified chunkserver from the master's metadata. Usually called when we
    # detect that this chunkserver has failed or has stopped responding.
    def remove_chunkserver(self, url_to_remove):
        # remove url of failed chunkserver from chunk_to_urls and chunkserver_url_to_proxy
        if url_to_remove not in self.chunkserver_url_to_proxy:
            return
        del self.chunkserver_url_to_proxy[url_to_remove]
        for chunk_id in self.chunk_to_urls:
            replica_list = self.chunk_to_urls[chunk_id]
            new_list = []
            for replica_url in replica_list:
                if replica_url == url_to_remove:
                    continue
                new_list.append(replica_url)
            self.chunk_to_urls[chunk_id] = new_list
        print('removed failed chunkserver at', url_to_remove)
        print('remaining chunk_to_urls:', self.chunk_to_urls)

    # lookup_chunk_version:
    # Looks up the version of the specified chunk id. Used for stale replica detection.
    def lookup_chunk_version(self, chunk_id):
        filename = self.chunk_to_filename[chunk_id]
        file_info = self.filename_to_chunks[filename]
        cur_version = None
        chunk_idx = None
        for i, (cur_id, v) in enumerate(file_info.chunk_list):
            if cur_id == chunk_id:
                cur_version = v
                chunk_idx = i
                break
        return cur_version, chunk_idx

    # report_invalid_checksum:
    # Called by a chunkserver to report a chunk that it discovers has an invalid checksum.
    # The master then rereplicates that chunk to a different chunkserver.
    def report_invalid_checksum(self, chunk_id, bad_url):
        print(bad_url, 'has invalid checksum for', chunk_id, 'so rereplicating')
        all_urls = self.chunkserver_url_to_proxy.keys()
        valid_replicas = self.chunk_to_urls[chunk_id]
        urls_without_replicas = [url for url in all_urls if url not in valid_replicas]
        valid_replicas.remove(bad_url)
        self.chunk_to_urls[chunk_id] = valid_replicas
        self.replicate_single_chunk(chunk_id, valid_replicas, urls_without_replicas)

    # replicate_single_chunk:
    # Replicates a single chunk from an existing replica to a replica without that chunk.
    def replicate_single_chunk(self, chunk_id, replica_list, urls_without_replicas):
        while len(urls_without_replicas) > 0:
            url = urls_without_replicas.pop()
            # pick random replica to copy from
            replica = random.choice(replica_list)
            cs_proxy = self.chunkserver_url_to_proxy[url]
            version, _ = self.lookup_chunk_version(chunk_id)
            res = cs_proxy.replicate_data(chunk_id, version, replica)
            if res == 'success':
                self.chunk_to_urls[chunk_id].append(url)
                print('re-replicated chunk', chunk_id, 'from', replica, 'to', url)
                break
            elif len(urls_without_replicas) == 0:
                print('not enough chunkservers to replicate chunk')
                break
            else:
                print('chunk replication failed:', res, 'trying on different replica')

    # rereplicate_chunks:
    # Periodically called to rereplicate all chunks with # replicas fewer
    # than self.num_replicas.
    def rereplicate_chunks(self):
        # loop through self.chunk_to_urls
        # for every chunk id with < self.num_replicas replicas
        #   replicate this chunk to another chunkserver
        for chunk_id in self.chunk_to_urls:
            replica_list = self.chunk_to_urls[chunk_id]
            if len(replica_list) >= self.num_replicas:
                continue
            all_urls = self.chunkserver_url_to_proxy.keys()
            # get all urls that don't contain a replica for this chunk
            urls_without_replicas = [url for url in all_urls if url not in replica_list]
            for i in range(len(replica_list), self.num_replicas):
                self.replicate_single_chunk(chunk_id, replica_list, urls_without_replicas)

    # create:
    # Called by the client to create a file with the desired filename.
    # Sets up metadata for the file and creates it on a replicated number of chunkservers.
    def create(self, filename):
        #randomly sample self.num_replicas servers to host replicas on
        all_urls = self.chunkserver_url_to_proxy.keys()
        proxy_urls = random.sample(all_urls, self.num_replicas)
        #assign next chunkId to each chunk sequentially
        chunk_id = self.chunk_id_counter
        replica_urls = []
        for url in proxy_urls:
            proxy = self.chunkserver_url_to_proxy[url]
            try:
                proxy.create(filename, chunk_id)
            except Exception as e:
                print('create failed:', e)
                self.remove_chunkserver(url)
                continue
            replica_urls.append(url)
        #store chunkId->list[server] mapping in chunk_to_url
        self.chunk_to_urls[chunk_id] = replica_urls
        self.chunk_to_filename[chunk_id] = filename
        self.chunk_id_counter += 1
        #append this chunkId to list of chunkIds in filename_to_chunks
        if filename not in self.filename_to_chunks:
            self.filename_to_chunks[filename] = FileInfo(chunk_list=[])
        self.filename_to_chunks[filename].chunk_list.append((chunk_id, 0))
        print('after CREATE filename_to_chunks:', self.filename_to_chunks)
        print('chunk_to_urls:', self.chunk_to_urls)
        self.flush_to_log()
        return 'successfully created ' + filename


    # delete:
    # Called by the client to delete the specified filename. All the master does is
    # rename the filename to DELETED_original_filename. The background process will
    # delete the metadata for the file after the specified deletion time expires.
    def delete(self, filename):
        if filename not in self.filename_to_chunks:
            return 'file not found'
        chunk_list = self.filename_to_chunks[filename].chunk_list[:]
        del self.filename_to_chunks[filename]
        t = time.time()
        deleted_filename = 'DELETED_' + filename
        self.filename_to_chunks[deleted_filename] = FileInfo(True, t, chunk_list)
        print('after DELETE filename_to_chunks:', self.filename_to_chunks)
        self.flush_to_log()
        return 'successfully deleted ' + filename

    # read:
    # Called by the client to get metadata for a chunkserver read.
    def read(self, filename, chunk_idx):
        if filename not in self.filename_to_chunks:
            return 'file not found'
        if chunk_idx > len(self.filename_to_chunks[filename].chunk_list) - 1:
            return 'requested chunk idx out of range'
        chunk_id, version = self.filename_to_chunks[filename].chunk_list[chunk_idx]
        replica_urls = self.chunk_to_urls[chunk_id]
        print('READ returning:', chunk_id, replica_urls)
        return (chunk_id, replica_urls)

    def get_replicas(self, filename, chunk_idx, force_update):
        if filename not in self.filename_to_chunks:
            return 'file not found'
        chunk_id, version = self.filename_to_chunks[filename].chunk_list[chunk_idx]
        replica_urls = self.chunk_to_urls[chunk_id]

        if force_update :
            # update version number of chunk and notify replicas
            chunk_id, version = self.filename_to_chunks[filename].chunk_list[chunk_idx]
            self.filename_to_chunks[filename].chunk_list[chunk_idx] = (chunk_id, version+1)
            urls_to_remove = []
            for url in replica_urls:
                try:
                    replica_proxy = self.chunkserver_url_to_proxy[url]
                    replica_proxy.update_version(chunk_id, version+1)
                except:
                    urls_to_remove.append(url)
                    self.remove_chunkserver(url)
                    continue
            for url in urls_to_remove:
                replica_urls.remove(url)

            if len(replica_urls) == 0:
                return 'no replicas remaining for chunk id ' + str(chunk_id)
            self.flush_to_log()
        else:
            print('no update needed for chunk id', chunk_id)

        return (chunk_id, replica_urls)

#Main
def main():
    #number of replicas
    n_replicas = int(sys.argv[1])
    if(n_replicas < 3):
        print("Invalid number of replicas")
        return

    master_server = SimpleXMLRPCServer(('10.172.0.3', 9000), allow_none=True)
    master_server.register_introspection_functions()
    master_server.register_instance(Master(n_replicas))
    master_server.serve_forever()

if __name__ == '__main__':
    main()
