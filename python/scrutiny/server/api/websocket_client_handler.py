import websockets
import queue
import time 
import asyncio
import threading
import uuid
import logging
import json

class Timer:
    def __init__(self, timeout, callback, *args, **kwargs):
        self.timeout = timeout
        self.callback = callback
        self.args = args
        self.kwargs = kwargs
        self.start()

    async def job(self):
        await asyncio.sleep(self.timeout)   #wait
        try:
            await self.callback()
            self.start()
        except:
            raise e     #fixme:  Exception raised in callback are lost.. some asyncio shenanigan required.

    def start(self):
        self.task = asyncio.ensure_future(self.job(*self.args, **self.kwargs))

    def cancel(self):
        self.task.cancel()


class WebsocketClientHandler:
    def __init__(self, config):
        self.rxqueue = queue.Queue()
        self.txqueue = queue.Queue()
        self.config = config
        self.loop = asyncio.new_event_loop()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.id2ws_map = dict()
        self.ws2id_map = dict()
        self.ws_server = None
        self.started_event = threading.Event()  # This event synchronise the start of the server

    async def register(self, websocket):
        wsid = self.make_id()
        self.id2ws_map[wsid] = websocket
        self.ws2id_map[websocket] = wsid
        return wsid

    async def unregister(self, websocket):
        wsid = self.ws2id_map[websocket]
        del self.ws2id_map[websocket]
        del self.id2ws_map[wsid]

    def is_connection_active(self, conn_id):
        return True if conn_id in self.id2ws_map else False
            
    #Executed for each websocket
    async def server_routine(self, websocket, path):
        wsid = await self.register(websocket)
        tx_sync_timer = Timer(0.05, self.process_tx_queue)

        try:
            async for message in websocket:
                try:
                    obj = json.loads(message)
                    self.rxqueue.put({'conn_id' : wsid, 'obj' : obj})
                except Exception as e:
                    self.logger.error('Received malformed JSON. %s' % str(e))
                    self.logger.debug(message)
        finally:
            tx_sync_timer.cancel()
            await self.unregister(websocket)


    async def process_tx_queue(self):
        while not self.txqueue.empty():
            popped = self.txqueue.get()
            if 'conn_id' not in popped or 'obj' not in popped:
                continue
            wsid = popped['conn_id']
            if wsid not in self.id2ws_map:
                continue
            websocket = self.id2ws_map[wsid]
            try:
                msg = json.dumps(popped['obj'])
                await websocket.send(msg)
            except Exception as e:
                self.logger.error('Cannot send message. Invalid JSON. %s' % str(e) )

    def process(self):
        pass #nothing to do

    # Run in client_handler thread
    def run(self):
        asyncio.set_event_loop(self.loop)
        self.ws_server = websockets.serve(self.server_routine, self.config['host'], self.config['port'])

        self.logger.info('Starting websocket listener')
        self.loop.run_until_complete(self.ws_server)
        self.started_event.set()
        self.loop.run_forever()

    # Called from Main Thread
    def start(self):
        self.started_event.clear()
        self.thread = threading.Thread(target=self.run)
        self.thread.start()
        self.started_event.wait()   # Wait for the websocket server to start. Avoid race conditions

    # Called from Main Thread
    def stop(self):
        self.logger.info('Stopping websocket listener')
        self.loop.call_soon_threadsafe(self.stop_from_thread)
        if  self.thread is not None:
            self.thread.join()
    
    # Called from client_handler Thread
    def stop_from_thread(self):
        asyncio.ensure_future(self.async_close())

    async def async_close(self):
        x = self.ws_server.ws_server.close()
        await self.ws_server.ws_server.wait_closed()
        self.loop.stop()
        self.logger.info('Websocket listener stopped')

    def send(self, conn_id, obj):
        if not self.txqueue.full():
            container = {'conn_id' : conn_id, 'obj' : obj}
            self.txqueue.put(container)

    def available(self):
        return not self.rxqueue.empty()

    def recv(self):
        try:
            return self.rxqueue.get_nowait()
        except:
            pass

    def make_id(self):
        return uuid.uuid4().hex

