#!/usr/bin/env python3
# ==============================================================================
# CLI de Control Interactivo para Mesh OS (Consola de Administración)
# Se conecta a la API de control loopback (puerto 5559) del nodo local.
# Solo usa la biblioteca estándar de Python.
# ==============================================================================

import socket
import sys
import json
import struct
import cmd
from typing import Optional

CTRL_PORT = 5559
BUFFER = 65535

def encode(msg: dict) -> bytes:
    raw = json.dumps(msg, separators=(',', ':')).encode()
    return struct.pack('>I', len(raw)) + raw

def decode_tcp(sock) -> Optional[dict]:
    try:
        h = b''
        while len(h) < 4:
            c = sock.recv(4 - len(h))
            if not c: return None
            h += c
        length = struct.unpack('>I', h)[0]
        if length > 10_000_000: return None
        d = b''
        while len(d) < length:
            c = sock.recv(min(length - len(d), BUFFER))
            if not c: return None
            d += c
        return json.loads(d.decode())
    except Exception:
        return None

def send_cmd(cmd_name: str, args: dict = None) -> dict:
    if args is None:
        args = {}
    payload = {'cmd': cmd_name}
    payload.update(args)
    
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3.0)
        s.connect(('127.0.0.1', CTRL_PORT))
        s.sendall(encode(payload))
        resp = decode_tcp(s)
        s.close()
        if resp is None:
            return {'error': 'Respuesta vacía o error de decodificación'}
        return resp
    except socket.timeout:
        return {'error': 'Tiempo de espera agotado al conectar al nodo'}
    except ConnectionRefusedError:
        return {'error': 'Conexión rechazada. ¿Está el nodo mesh.node (python3 -m mesh.node) corriendo en este dispositivo?'}
    except Exception as e:
        return {'error': f'Error de socket: {e}'}

class MeshCLI(cmd.Cmd):
    intro = """
╔══════════════════════════════════════════════════════════════╗
║              CONSOLA DE CONTROL DE MESA (Mesh OS)             ║
║  Escriba 'help' o '?' para listar los comandos disponibles.  ║
╚══════════════════════════════════════════════════════════════╝
"""
    prompt = 'mesh> '

    def do_status(self, arg):
        """Muestra el estado del nodo local: batería, carga, reputación y contadores."""
        resp = send_cmd('status')
        if 'error' in resp:
            print(f"[Error] {resp['error']}")
            return
        
        print("\n=== ESTADO DEL NODO LOCAL ===")
        print(f" ID del Nodo : N{resp.get('node_id')}")
        print(f" Batería     : {resp.get('battery'):.1f}%")
        print(f" Carga Actual: {resp.get('load')}/4 tareas")
        print(f" Reputación  : {resp.get('reputation'):.3f}")
        print(f" Tareas OK   : {resp.get('tasks_done')}")
        print(f" Vecinos     : {resp.get('peers')}")
        print(f" Rutas Malla : {resp.get('routes')}")
        print(f" Items Mem.  : {resp.get('mem_size')}")
        print(f" Cree Caídos : {', '.join(map(str, resp.get('failed', []))) or '(ninguno)'}")
        print("==============================\n")

    def do_peers(self, arg):
        """Lista la tabla de vecinos conocidos (PeerInfo) por broadcast UDP."""
        resp = send_cmd('peers')
        if 'error' in resp:
            print(f"[Error] {resp['error']}")
            return
        
        peers = resp.get('peers', [])
        if not peers:
            print("No se han detectado vecinos por el momento.")
            return
            
        print("\n=== VECINOS DETECTADOS (PeerInfo) ===")
        headers = ["ID", "Dirección IP", "Visto hace", "TQ", "Saltos", "Bat%", "Carga", "Reputación", "Estado"]
        col_w = [4, 15, 12, 6, 6, 6, 6, 12, 10]
        
        print("  ".join(headers[i].ljust(col_w[i]) for i in range(len(headers))))
        print("  ".join("-" * w for w in col_w))
        
        for p in sorted(peers, key=lambda x: x['node_id']):
            status_str = "CAÍDO" if p.get('lost') else ("ALERTA" if p.get('in_alert') else "OK")
            row = [
                f"N{p['node_id']}",
                p['ip'],
                f"{p['last_seen']:.1f}s",
                f"{p['tq']:.2f}",
                str(p['hops']),
                f"{p['battery']:.0f}%",
                str(p['load']),
                f"{p['reputation']:.3f}",
                status_str
            ]
            print("  ".join(row[i].ljust(col_w[i]) for i in range(len(row))))
        print("======================================\n")

    def do_routes(self, arg):
        """Muestra la tabla de enrutamiento del protocolo B.A.T.M.A.N."""
        resp = send_cmd('routes')
        if 'error' in resp:
            print(f"[Error] {resp['error']}")
            return
            
        routes = resp.get('routes', [])
        if not routes:
            print("Sin rutas activas en la tabla BATMAN.")
            return
            
        print("\n=== TABLA DE RUTAS B.A.T.M.A.N. ===")
        headers = ["Destino", "Siguiente Salto", "Hops", "TQ (Calidad)", "Secuencia", "Visto hace"]
        col_w = [9, 17, 6, 12, 10, 12]
        
        print("  ".join(headers[i].ljust(col_w[i]) for i in range(len(headers))))
        print("  ".join("-" * w for w in col_w))
        
        for r in sorted(routes, key=lambda x: x['dest']):
            row = [
                f"N{r['dest']}",
                f"{r['via_ip']} (N{r['via_id']})",
                str(r['hops']),
                f"{r['tq']:.2f}",
                str(r['seq']),
                f"{time_diff(r['last_seen']):.1f}s"
            ]
            print("  ".join(row[i].ljust(col_w[i]) for i in range(len(row))))
        print("=====================================\n")

    def do_mem(self, arg):
        """Muestra la memoria distribuida.
Uso:
  mem                  # Listar todas las llaves y valores
  mem <llave>          # Leer un valor específico
"""
        arg = arg.strip()
        if arg:
            resp = send_cmd('mem_read', {'key': arg})
            if 'error' in resp:
                print(f"[Error] {resp['error']}")
                return
            val = resp.get('value')
            if val is None:
                print(f"La llave '{arg}' no existe en la memoria distribuida.")
            else:
                print(f"{arg} = {json.dumps(val, indent=2)}")
        else:
            resp = send_cmd('mem_read')
            if 'error' in resp:
                print(f"[Error] {resp['error']}")
                return
            entries = resp.get('entries', {})
            if not entries:
                print("Memoria distribuida vacía.")
                return
            print("\n=== MEMORIA DISTRIBUIDA CAUSAL ===")
            for k, e in sorted(entries.items()):
                print(f" {k:<25} -> {json.dumps(e.get('value'))}  (Autor: N{e.get('author')}, Ver: {e.get('version')})")
            print("==================================\n")

    def do_memw(self, arg):
        """Escribe una llave y su valor en la memoria distribuida.
Uso: memw <llave> <valor_json_o_string>
Ejemplo: memw sensor.temp 22.5
         memw mision.estado "completo"
"""
        parts = arg.strip().split(maxsplit=1)
        if len(parts) < 2:
            print("Uso: memw <llave> <valor>")
            return
        key, raw_val = parts[0], parts[1]
        try:
            val = json.loads(raw_val)
        except ValueError:
            val = raw_val  # Si no es JSON válido, lo guardamos como string
            
        resp = send_cmd('mem_write', {'key': key, 'value': val})
        if 'error' in resp:
            print(f"[Error] {resp['error']}")
        elif resp.get('ok'):
            print(f"Estructura guardada: '{key}' de manera causal en la red.")
        else:
            print("[Error] Falló al guardar en memoria local.")

    def do_task(self, arg):
        """Somete una tarea de Machine Learning al enrutador distribuido.
El planificador la enviará al nodo con mejor score.
Uso: task <tipo_tarea> [argumentos_json]
Tipos de tarea predefinidos:
  task mlp        - Red Neuronal Artificial (Demo XOR)
  task linreg     - Regresión Lineal
  task sfusion    - Fusión de Sensores (ponderación por varianza)
  task astar      - Planificación de Rutas A*
  task logreg     - Regresión Logística
  task svm        - Support Vector Machine
  task dectree    - Árbol de Decisión

Ejemplos:
  task mlp
  task astar {"grid":[[0,0],[0,0]], "start":[0,0], "goal":[1,1]}
"""
        parts = arg.strip().split(maxsplit=1)
        if not parts:
            print("Debe especificar un tipo de tarea. Ej: task mlp")
            return
        
        task_type_in = parts[0].lower()
        payload = {}
        
        # Mapeo a tipos reales de TaskType
        task_map = {
            'linreg': 'LinReg',
            'logreg': 'LogReg',
            'svm': 'SVM',
            'dectree': 'DecTree',
            'mlp': 'MLP',
            'sfusion': 'SensorFusion',
            'astar': 'PathPlan'
        }
        
        if task_type_in not in task_map:
            print(f"Tipo de tarea desconocido. Elija entre: {', '.join(task_map.keys())}")
            return
            
        task_type = task_map[task_type_in]
        
        # Cargar payload personalizado si se provee
        if len(parts) > 1:
            try:
                payload = json.loads(parts[1])
            except ValueError:
                print("[Error] Argumentos de tarea deben ser JSON válido.")
                return
        else:
            # Cargar defaults
            if task_type == 'MLP':
                payload = {
                    'X': [[0,0],[0,1],[1,0],[1,1]],
                    'y': [0, 1, 1, 0],
                    'hidden': 4,
                    'lr': 0.1,
                    'epochs': 1000
                }
            elif task_type == 'LinReg':
                payload = {
                    'X': [[1],[2],[3],[4],[5]],
                    'y': [1.9, 4.1, 6.0, 7.9, 10.1],
                    'lr': 0.01,
                    'epochs': 500
                }
            elif task_type == 'SensorFusion':
                payload = {
                    'readings': {
                        'co2': [400, 410, 395, 405],
                        'temp': [23.1, 23.5, 22.9, 23.2]
                    }
                }
            elif task_type == 'PathPlan':
                payload = {
                    'grid': [
                        [0,0,0,1,0],
                        [0,1,0,1,0],
                        [0,1,0,0,0],
                        [0,0,0,1,0],
                        [1,1,0,0,0]
                    ],
                    'start': [0,0],
                    'goal': [4,4]
                }
            elif task_type == 'LogReg':
                payload = {
                    'X': [[1,1],[2,1],[1,2],[4,4],[5,4],[4,5]],
                    'y': [0, 0, 0, 1, 1, 1],
                    'lr': 0.1,
                    'epochs': 300
                }
            elif task_type == 'SVM':
                payload = {
                    'X': [[1,2],[2,3],[5,5],[6,5]],
                    'y': [-1, -1, 1, 1],
                    'lr': 0.01,
                    'C': 1.0,
                    'epochs': 200
                }
            elif task_type == 'DecTree':
                payload = {
                    'X': [[1,1],[1,0],[0,1],[0,0]],
                    'y': [1, 1, 0, 0],
                    'max_depth': 3
                }

        print(f"Enviando tarea {task_type} con payload: {json.dumps(payload)}")
        resp = send_cmd('submit_task', {'task_type': task_type, 'payload': payload})
        if 'error' in resp:
            print(f"[Error] {resp['error']}")
            return
            
        print("\n=== TAREA ENVIADA CON ÉXITO ===")
        print(f" ID Asignado: {resp.get('task_id')}")
        print(f" Estado     : {resp.get('state')}")
        dest_str = f"N{resp.get('assigned_to')}" if resp.get('assigned_to') != -1 else "Sin asignar (cola)"
        print(f" Asignada a : {dest_str}")
        print("===============================\n")

    def do_tasks(self, arg):
        """Muestra la lista de tareas en ejecución o finalizadas en el clúster."""
        resp = send_cmd('tasks')
        if 'error' in resp:
            print(f"[Error] {resp['error']}")
            return
            
        tasks = resp.get('tasks', [])
        if not tasks:
            print("No hay tareas registradas en el clúster.")
            return
            
        print("\n=== REGISTRO DE TAREAS DISTRIBUIDAS ===")
        headers = ["ID Tarea", "Tipo", "Origen", "Prioridad", "Asignado A", "Estado", "Resultado Resumido"]
        col_w = [12, 14, 8, 10, 12, 10, 30]
        
        print("  ".join(headers[i].ljust(col_w[i]) for i in range(len(headers))))
        print("  ".join("-" * w for w in col_w))
        
        for t in sorted(tasks, key=lambda x: x['created_at']):
            dest = f"N{t['assigned_to']}" if t['assigned_to'] != -1 else "-"
            res = json.dumps(t.get('result', {}))
            if len(res) > 28:
                res = res[:25] + "..."
            row = [
                t['id'],
                t['task_type'],
                f"N{t['origin_id']}",
                str(t['priority']),
                dest,
                t['state'].upper(),
                res
            ]
            print("  ".join(row[i].ljust(col_w[i]) for i in range(len(row))))
        print("=======================================\n")

    def do_fault(self, arg):
        """Muestra el log de fallos de la red (FaultManager)."""
        resp = send_cmd('fault_log')
        if 'error' in resp:
            print(f"[Error] {resp['error']}")
            return
            
        log = resp.get('log', [])
        failed = resp.get('failed', [])
        
        print("\n=== NODO(S) DETECTADO(S) COMO CAÍDO(S) ===")
        print(", ".join(f"N{nid}" for nid in failed) if failed else "(ninguno)")
        print("=========================================")
        
        print("\n=== HISTORIAL RECIENTE DE FALLOS ===")
        if not log:
            print("  Sin eventos de fallos registrados.")
        else:
            for item in log:
                print(f"  [{time_diff_str(item.get('ts', 0))}] {item.get('msg')}")
        print("====================================\n")

    def do_ping(self, arg):
        """Realiza una prueba básica de conectividad TCP con otro nodo de la malla.
Uso: ping <ip_destino> [puerto]
Ejemplo: ping 192.168.99.2
"""
        parts = arg.strip().split()
        if not parts:
            print("Uso: ping <ip_destino> [puerto]")
            return
        ip = parts[0]
        port = int(parts[1]) if len(parts) > 1 else CTRL_PORT
        
        print(f"Probando conexión a {ip}:{port}...")
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            start = socket.getaddrinfo(ip, port)
            s.connect((ip, port))
            s.close()
            print(f"¡CONECTADO! Puerto {port} en {ip} está activo y respondiendo.")
        except Exception as e:
            print(f"FALLO DE CONEXIÓN: {e}")

    def do_exit(self, arg):
        """Sale del CLI de control."""
        print("Saliendo de la consola de control.")
        return True

    def do_quit(self, arg):
        """Sale del CLI de control."""
        return self.do_exit(arg)

def time_diff(ts: float) -> float:
    import time
    return max(0.0, time.time() - ts)

def time_diff_str(ts: float) -> str:
    import time
    diff = time.time() - ts
    if diff < 60:
        return f"hace {diff:.0f}s"
    return time.strftime("%H:%M:%S", time.localtime(ts))

if __name__ == '__main__':
    try:
        # Intentar una consulta status rápida para ver si el nodo local está corriendo
        test = send_cmd('status')
        if 'error' in test and 'Conexión rechazada' in test['error']:
            print(test['error'])
            sys.exit(1)
        MeshCLI().cmdloop()
    except KeyboardInterrupt:
        print("\nSaliendo.")
