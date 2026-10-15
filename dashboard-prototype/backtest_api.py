"""Allowlisted job and report endpoints. Authentication stays at the existing proxy."""
import json
from urllib.parse import urlsplit
from backtest_jobs import Jobs, catalog, validate, ARTIFACTS


def route(handler,method):
    path=urlsplit(handler.path).path
    if not path.startswith('/api/backtest'):return False
    try:
        jobs=Jobs()
        if method=='GET' and path=='/api/backtest/catalog':handler.respond(200,catalog())
        elif method=='GET' and path=='/api/backtest/jobs':handler.respond(200,{'jobs':jobs.rows()})
        elif method=='POST' and path=='/api/backtest/jobs':
            if handler.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('Требуется application/json')
            size=int(handler.headers.get('Content-Length','0'))
            if not 0<size<=32768:raise ValueError('Размер запроса должен быть от 1 до 32768 байт')
            payload=json.loads(handler.rfile.read(size))
            handler.respond(202,jobs.submit(validate(payload)))
        else:
            parts=path.split('/')
            if len(parts)<5 or parts[3]!='jobs':raise KeyError(path)
            job=jobs.get(parts[4])
            if len(parts)==5 and method=='GET':handler.respond(200,job)
            elif len(parts)==6 and parts[5]=='cancel' and method=='POST':handler.respond(200,jobs.cancel(job['id']))
            elif len(parts)==7 and parts[5]=='files' and parts[6] in ARTIFACTS and method=='GET':
                file=jobs.root/'runs'/job['id']/parts[6]
                if job['status']!='succeeded' or not file.is_file():raise KeyError(path)
                handler.respond(200,file.read_bytes(),ARTIFACTS[parts[6]])
            else:raise KeyError(path)
    except KeyError:handler.respond(404,{'error':'Запуск или файл не найден'})
    except (ValueError,TypeError,AttributeError) as exc:handler.respond(400,{'error':str(exc)[:1500]})
    except (OSError,ImportError):handler.respond(503,{'error':'Сервис расчёта недоступен. Проверьте worker и конфигурацию данных.'})
    return True
