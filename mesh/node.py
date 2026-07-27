import json
import logging
import os
import signal
import socket
import struct
import sys
import threading
import time
import uuid
from typing import Optional

from mesh.api import CtrlAPI
from mesh.fault_manager import FaultManager
from mesh.memory import DistributedMemory
from mesh.router import BatmanRouter
from mesh.scheduler import DistributedScheduler


def encode_udp(msg: dict) -> bytes:
    return json.dumps(msg, separators=(',', ':')).encode()

def decode_udp(data: bytes) -> Optional[dict]:
    try:    return json.loads(data.decode())
    except: return None

# ══════════════════════════════════════════════════════════════════════
#  NODO PRINCIPAL 
# ══════════════════════════════════════════════════════════════════════
class MeshNode:
    def __init__(self, node_id: int, interface: str,
                 bind_ip: str = "0.0.0.0",
                 data_dir: str = "./mesh_data",
                 BCAST_PORT=5555,
                 UNICAST_PORT=5556,
                 MEM_PORT=5557,
                 CTRL_PORT=5559,
                 BATMAN_CADA=4.0,
                 BEACON_CADA=2.0,
                 MEM_SYNC_CADA=8.0,
                 TIMEOUT_ALERT=15.0,
                 BATMAN_TTL=6,
                 MAX_LOAD=4,
                 BCAST_ADDR="255.255.255.255",
                 BUFFER=65535
                 ):
        self.node_id   = node_id
        self.interface = interface
        self.bind_ip   = bind_ip
        self.BCAST_PORT = BCAST_PORT
        self.UNICAST_PORT = UNICAST_PORT
        self.MEM_PORT = MEM_PORT
        self.CTRL_PORT = CTRL_PORT
        self.BATMAN_CADA = BATMAN_CADA
        self.BEACON_CADA = BEACON_CADA
        self.MEM_SYNC_CADA = MEM_SYNC_CADA
        self.TIMEOUT_ALERT = TIMEOUT_ALERT
        self.BATMAN_TTL = BATMAN_TTL
        self.MAX_LOAD = MAX_LOAD
        self.BCAST_ADDR = BCAST_ADDR
        self.BUFFER = BUFFER
        os.makedirs(data_dir, exist_ok=True)

        self.router    = BatmanRouter(node_id)
        self.scheduler = DistributedScheduler(node_id,self.MAX_LOAD)
        self.memory    = DistributedMemory(node_id,
                         path=os.path.join(data_dir, f"mem_n{node_id}.json"))
        self.fault_mgr = FaultManager(node_id)
        self.ctrl_api  = CtrlAPI(self,self.CTRL_PORT)

        self._running  = True
        self._battery  = self._read_bat()
        self._ogm_seq  = 0
        self.log = logging.getLogger(f"Node[N{node_id}]")

    # ── Arranque ─────────────────────────────────────────────────────
    def start(self):
        hilos = [
            ("beacon-tx",   self._beacon_tx),
            ("ogm-tx",      self._ogm_tx),
            ("bcast-rx",    self._bcast_rx),
            ("unicast-srv", self._unicast_srv),
            ("mem-sync",    self._mem_sync),
            ("task-worker", self._task_worker),
            ("fault-watch", self._fault_watch),
            ("battery-mon", self._bat_loop),
        ]
        for name, fn in hilos:
            threading.Thread(target=fn, name=name, daemon=True).start()
        self.ctrl_api.start()
        signal.signal(signal.SIGINT,  self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)
        self.log.info(f"Nodo N{self.node_id} activo — interfaz: {self.interface}")

    def run_forever(self):
        self.start()
        while self._running: time.sleep(1)

    def _shutdown(self, *_):
        self.log.info("Apagando..."); self._running = False; sys.exit(0)

    # ── Batería ───────────────────────────────────────────────────────
    def _read_bat(self) -> float:
        for p in ["/sys/class/power_supply/BAT0/capacity",
                  "/sys/class/power_supply/BAT1/capacity",
                  "/sys/class/power_supply/battery/capacity"]:
            try:
                with open(p) as f: return float(f.read().strip())
            except: pass
        return 100.0

    def _bat_loop(self):
        while self._running:
            self._battery = self._read_bat(); time.sleep(30)

    # ── TX: Beacon UDP ────────────────────────────────────────────────
    def _beacon_tx(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        while self._running:
            msg = {'type':'BCN','node_id':self.node_id,
                   'battery':self._battery,'load':self.scheduler.load,
                   'rep':self.scheduler.reputation,'ts':time.time()}
            try: s.sendto(encode_udp(msg), (self.BCAST_ADDR, self.BCAST_PORT))
            except Exception as e: self.log.warning(f"Beacon: {e}")
            time.sleep(self.BEACON_CADA)
        s.close()

    # ── TX: OGM UDP ───────────────────────────────────────────────────
    def _ogm_tx(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        while self._running:
            self._ogm_seq += 1
            ogm = {'type':'OGM','origin_id':self.node_id,'seq':self._ogm_seq,
                   'ttl':self.BATMAN_TTL,'path':[self.node_id],
                   'battery':self._battery,'load':self.scheduler.load,
                   'reputation':self.scheduler.reputation,'tq':1.0,
                   'survivors':[],'alerts':list(self.fault_mgr.failed),
                   'mem_digest':self.memory.digest(),'ts':time.time()}
            try: s.sendto(encode_udp(ogm), (self.BCAST_ADDR, self.BCAST_PORT))
            except Exception as e: self.log.warning(f"OGM TX: {e}")
            time.sleep(self.BATMAN_CADA)
        s.close()

    # ── RX: Broadcast UDP ─────────────────────────────────────────────
    def _bcast_rx(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(('', self.BCAST_PORT)); s.settimeout(2.0)
        self.log.info(f"Escuchando broadcast :{self.BCAST_PORT}")
        while self._running:
            try:
                data, (from_ip, _) = s.recvfrom(self.BUFFER)
                msg = decode_udp(data)
                if not msg: continue
                sid = msg.get('node_id') or msg.get('origin_id')
                if sid == self.node_id: continue
                self._handle_bcast(msg, from_ip)
            except socket.timeout: continue
            except Exception as e: self.log.error(f"RX bcast: {e}")
        s.close()

    def _handle_bcast(self, msg: dict, from_ip: str):
        now = time.time(); mt = msg.get('type')
        if mt == 'BCN':
            nid = msg['node_id']
            with self.router._lock:
                if nid not in self.router.peers:
                    self.router.peers[nid] = self.router.PeerInfo(
                        node_id=nid, ip=from_ip, last_seen=now)
                p = self.router.peers[nid]
                p.last_seen  = now;  p.ip         = from_ip
                p.battery    = msg.get('battery',   100.0)
                p.load       = msg.get('load',       0)
                p.reputation = msg.get('rep',        1.0)
                if p.in_alert:
                    p.in_alert = False
                    self.fault_mgr.recover(nid, now)

        elif mt == 'OGM':
            is_new = self.router.receive_ogm(msg, from_ip, now)
            if is_new and msg.get('ttl', 0) > 1:
                fwd = dict(msg)
                fwd['ttl']  = msg['ttl'] - 1
                fwd['path'] = msg['path'] + [self.node_id]
                fwd['tq']   = msg.get('tq',1.0) * self.router.link_quality(from_ip)
                try:
                    sf = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sf.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    sf.sendto(encode_udp(fwd), (self.BCAST_ADDR, self.BCAST_PORT))
                    sf.close()
                except: pass
            for aid in msg.get('alerts', []):
                self.router.mark_alert(aid)

    # ── Servidor TCP: tareas y resultados ─────────────────────────────
    def _unicast_srv(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.bind_ip, self.UNICAST_PORT))
        srv.listen(10); srv.settimeout(2.0)
        self.log.info(f"TCP tareas :{self.UNICAST_PORT}")
        while self._running:
            try:
                conn, addr = srv.accept()
                threading.Thread(target=self._handle_tcp,
                                 args=(conn,addr), daemon=True).start()
            except socket.timeout: continue
            except Exception as e: self.log.error(f"TCP accept: {e}")
        srv.close()

    def _handle_tcp(self, conn, addr):
        try:
            msg = self.ctrl_api.decode_tcp(conn)
            if not msg: return
            mt = msg.get('type')
            if mt == 'TASK':
                t = self.scheduler.from_dict(msg['task'])
                self.scheduler.accept(t)
                conn.sendall(self.ctrl_api.encode({'status':'accepted','task_id':t.id}))
                threading.Thread(target=self._exec_report,
                                 args=(t, addr[0]), daemon=True).start()
            elif mt == 'TRES':
                tid = msg['task_id']
                if tid in self.scheduler.tasks:
                    self.scheduler.tasks[tid].result = msg['result']
                    self.scheduler.tasks[tid].state  = self.scheduler.TaskState.DONE
                conn.sendall(self.ctrl_api.encode({'status':'ok'}))
            elif mt == 'PING':
                conn.sendall(self.ctrl_api.encode({'type':'PONG','node_id':self.node_id,
                                     'battery':self._battery,
                                     'load':self.scheduler.load,
                                     'ts':time.time()}))
        except Exception as e: self.log.error(f"Handle TCP: {e}")
        finally: conn.close()

    def _exec_report(self, task, origin_ip: str):
        result = self.scheduler.execute(task)
        self.memory.write(f"result.{task.id}", result)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10); s.connect((origin_ip, self.UNICAST_PORT))
            s.sendall(self.ctrl_api.encode({'type':'TRES','task_id':task.id,
                              'result':result,'from_id':self.node_id}))
            s.close()
        except Exception as e:
            self.log.warning(f"No pude reportar {task.id}: {e}")

    # ── Sincronización de memoria ─────────────────────────────────────
    def _mem_sync(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.bind_ip, self.MEM_PORT))
        srv.listen(10); srv.settimeout(0.5)

        def accept_loop():
            while self._running:
                try:
                    conn,_ = srv.accept()
                    threading.Thread(target=self._handle_mem,
                                     args=(conn,), daemon=True).start()
                except socket.timeout: pass
                except: pass

        threading.Thread(target=accept_loop, daemon=True,
                         name="mem-acceptor").start()

        while self._running:
            time.sleep(self.MEM_SYNC_CADA)
            for peer in self.router.alive_peers(time.time(),self.TIMEOUT_ALERT):
                try: self._push_mem(peer.ip)
                except Exception as e:
                    self.log.debug(f"Mem sync N{peer.node_id}: {e}")
        srv.close()

    def _push_mem(self, ip: str):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(8); s.connect((ip, self.MEM_PORT))
        s.sendall(self.ctrl_api.encode({'type':'MREQ','node_id':self.node_id}))
        resp = self.ctrl_api.decode_tcp(s)
        if resp:
            entries = self.memory.newer_than(resp.get('digest',{}))
            if entries:
                s.sendall(self.ctrl_api.encode({'type':'MSYN','entries':entries,
                                  'from_id':self.node_id}))
                self.ctrl_api.decode_tcp(s)
        s.close()

    def _handle_mem(self, conn):
        try:
            msg = self.ctrl_api.decode_tcp(conn)
            if not msg: return
            if msg.get('type') == 'MREQ':
                conn.sendall(self.ctrl_api.encode({'digest':self.memory.digest()}))
                msg2 = self.ctrl_api.decode_tcp(conn)
                if msg2 and msg2.get('type') == 'MSYN':
                    merged = sum(1 for e in msg2.get('entries',[])
                                 if self.memory.merge(e))
                    conn.sendall(self.ctrl_api.encode({'merged':merged}))
                    if merged:
                        self.log.debug(f"Mem: integradas {merged} entradas")
        except: pass
        finally: conn.close()

    # ── Worker de tareas propias ──────────────────────────────────────
    def _task_worker(self):
        while self._running:
            time.sleep(1)
            with self.scheduler._lock:
                pending = [t for t in self.scheduler.tasks.values()
                           if t.assigned_to == self.node_id
                           and t.state == self.scheduler.TaskState.ASSIGNED]
            for task in pending:
                threading.Thread(target=self._exec_local,
                                 args=(task,), daemon=True).start()

    def _exec_local(self, task):
        result = self.scheduler.execute(task)
        self.memory.write(f"result.{task.id}", result)

    # ── Vigilancia de fallos ──────────────────────────────────────────
    def _fault_watch(self):
        while self._running:
            time.sleep(5)
            now   = time.time()
            peers = list(self.router.peers.values())
            failed = self.fault_mgr.check(peers, now,self.TIMEOUT_ALERT)
            for fid in failed:
                self.router.mark_alert(fid)
                alive = self.router.alive_peers(now,self.TIMEOUT_ALERT)
                n = self.fault_mgr.reassign(
                    fid, self.scheduler, alive, self._battery, now)
                if n: self.log.warning(f"Reasignadas {n} tareas de N{fid}")
                self.fault_mgr.repair_replicas(fid, self.memory, now)


    def submit_task(self, task_type: str, payload: dict,
                    priority: int = 2, deadline: float = 60.0):
        now  = time.time()
        task = self.scheduler.Task(
            id=f"T{self.node_id}-{uuid.uuid4().hex[:6].upper()}",
            task_type=task_type, origin_id=self.node_id,
            priority=priority, payload=payload,
            created_at=now, deadline=now+deadline)
        with self.scheduler._lock:
            self.scheduler.tasks[task.id] = task
        alive = self.router.alive_peers(now,self.TIMEOUT_ALERT)
        best  = self.scheduler.best_node(alive, self._battery)
        task.assigned_to = best
        if best == self.node_id:
            task.state = self.scheduler.TaskState.ASSIGNED
            self.log.info(f"Tarea {task.id} ({task_type}) → LOCAL")
        else:
            peer = self.router.peers.get(best)
            if peer:
                task.state = self.scheduler.TaskState.ASSIGNED
                threading.Thread(target=self._send_task,
                                 args=(task, peer.ip), daemon=True).start()
                self.log.info(
                    f"Tarea {task.id} ({task_type}) → N{best} ({peer.ip})")
            else:
                task.assigned_to = self.node_id
                task.state       = self.scheduler.TaskState.ASSIGNED
        return task

    def _send_task(self, task, ip: str):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10); s.connect((ip, self.UNICAST_PORT))
            s.sendall(self.ctrl_api.encode({'type':'TASK',
                              'task':self.scheduler.to_dict(task)}))
            self.ctrl_api.decode_tcp(s); s.close()
        except Exception as e:
            self.log.error(f"Envío tarea {task.id}: {e}")
            task.assigned_to = self.node_id
            task.state       = self.scheduler.TaskState.ASSIGNED

    def status(self) -> dict:
        return {'node_id':self.node_id,'battery':self._battery,
                'load':self.scheduler.load,
                'reputation':round(self.scheduler.reputation,3),
                'peers':len(self.router.peers),
                'routes':len(self.router.routes),
                'mem_size':self.memory.size(),
                'failed':list(self.fault_mgr.failed),
                'tasks_done':self.scheduler._done}

if __name__ == "__main__":
    import argparse
    
    p = argparse.ArgumentParser(description="Mesh OS · Nodo B.A.T.M.A.N.")
    p.add_argument('--id',        type=int, required=True,
                   help="ID único del nodo (entero positivo)")
    p.add_argument('--interface', default='wlan0',
                   help="Interfaz de red (wlan0, etc.)")
    p.add_argument('--bind',      default='0.0.0.0',
                   help="IP de bind para puertos TCP")
    p.add_argument('--data-dir',  default='./mesh_data',
                   help="Directorio de persistencia")
    p.add_argument('--demo',      action='store_true',
                   help="Inyectar tareas ML automáticamente cada 20s")
    p.add_argument('--log-level', default='INFO',
                   choices=['DEBUG','INFO','WARNING','ERROR'])
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    )

    # Puertos por defecto
    BCAST_PORT = 5555
    UNICAST_PORT = 5556
    MEM_PORT = 5557
    CTRL_PORT = 5559

    node = MeshNode(
        node_id=args.id,
        interface=args.interface,
        bind_ip=args.bind,
        data_dir=args.data_dir.replace('-', '_'),
        BCAST_PORT=BCAST_PORT,
        UNICAST_PORT=UNICAST_PORT,
        MEM_PORT=MEM_PORT,
        CTRL_PORT=CTRL_PORT
    )

    if args.demo:
        DEMO_TASKS = [
            ('linreg',   {'X':[[1],[2],[3],[4],[5]],'y':[2,4,5,4,5],
                          'lr':0.01,'epochs':500}),
            ('mlp',      {'X':[[0,0],[0,1],[1,0],[1,1]],'y':[0,1,1,0],
                          'lr':0.1,'epochs':1000,'hidden':4}),
            ('svm',      {'X':[[1,2],[2,3],[5,5],[6,5]],'y':[-1,-1,1,1],
                          'lr':0.001,'C':1.0,'epochs':200}),
            ('sfusion', {'readings':{
                'temp':[22.1,22.3,22.0,21.8],
                'co2':[410,412,409,411],
                'pressure':[1013.0,1012.5,1013.2]}}),
            ('astar', {'grid':[[0,0,0,0,0],[0,1,1,0,0],[0,0,0,1,0],
                                  [0,1,0,0,0],[0,0,0,0,0]],
                          'start':[0,0],'goal':[4,4]}),
        ]
        def demo_loop():
            time.sleep(10)
            i = 0
            while node._running:
                tt, pl = DEMO_TASKS[i % len(DEMO_TASKS)]
                t = node.submit_task(tt, pl, priority=2)
                node.memory.write(f"demo.task.{i}", t.id)
                i += 1; time.sleep(20)
        threading.Thread(target=demo_loop, daemon=True,
                         name="demo").start()

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║       Mesh OS · Nodo N{args.id} activo                            ║
╠══════════════════════════════════════════════════════════════╣
║  Interfaz : {args.interface:<48}║
║  Bind     : {args.bind:<48}║
╠══════════════════════════════════════════════════════════════╣
║  Puertos                                                     ║
║    UDP {BCAST_PORT}  → OGMs y Beacons (broadcast)              ║
║    TCP {UNICAST_PORT}  → Transferencia de tareas                ║
║    TCP {MEM_PORT}  → Sincronización de memoria              ║
║    TCP {CTRL_PORT}  → CLI de control (solo loopback)         ║
╠══════════════════════════════════════════════════════════════╣
║  En otra terminal:                                           ║
║    python3 mesh_cli.py                                       ║
║  Ctrl+C para apagar                                          ║
╚══════════════════════════════════════════════════════════════╝
""")
    node.run_forever()