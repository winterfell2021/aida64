import copy
import json
import os
import re
import time
from collections import defaultdict
from xml.etree import cElementTree as ET

import requests
from flask import Flask, render_template, request
from flask_gzip import Gzip
from flask_redis import FlaskRedis


def etree_to_dict(t):
    d = {t.tag: {} if t.attrib else None}
    children = list(t)
    if children:
        dd = defaultdict(list)
        for dc in map(etree_to_dict, children):
            for k, v in dc.items():
                dd[k].append(v)
        d = {t.tag: {k: v[0] if len(v) == 1 else v for k, v in dd.items()}}
    if t.attrib:
        d[t.tag].update(('@' + k, v) for k, v in t.attrib.items())
    if t.text:
        text = t.text.strip()
        if children or t.attrib:
            if text:
                d[t.tag]['#text'] = text
        else:
            d[t.tag] = text
    return d


gpu_name = os.popen("lspci | grep 'VGA' | grep '(?<=\[).*(?=\])' -oP").read().replace('\n', '')
cpu_name = re.search(r': (.*)CPU', os.popen("cat /proc/cpuinfo | grep 'model name' -m1").read()).group(1)

now = None
package_list = []


def init_power():
    global now, package_list
    now = time.time()
    base_dir = '/sys/class/powercap/intel-rapl/'
    packages = os.popen(f'ls {base_dir} | grep intel-rapl').read().splitlines()
    for package in packages:
        package_name = os.popen(f'cat {base_dir}{package}/name').read().replace('\n', '')
        package_energy = int(os.popen(f'cat {base_dir}{package}/energy_uj').read().replace('\n', ''))
        children_list = []
        child = os.popen(f'ls {base_dir}{package}/ | grep intel-rapl').read().splitlines()
        for vo in child:
            name = os.popen(f'cat {base_dir}{package}/{vo}/name').read().replace('\n', '')
            energy = int(os.popen(f'cat {base_dir}{package}/{vo}/energy_uj').read().replace('\n', ''))
            children_list.append({
                'name': name,
                'dir': f'{base_dir}{package}/{vo}/energy_uj',
                'energy': energy
            })
        package_list.append({
            'name': package_name,
            'dir': f'{base_dir}{package}/energy_uj',
            'energy': package_energy,
            'children': children_list
        })


class CustomFlask(Flask):
    jinja_options = Flask.jinja_options.copy()
    jinja_options.update(dict(
        variable_start_string='%%',  # Default is '{{', I'm changing this because Vue.js uses '{{' / '}}'
        variable_end_string='%%',
    ))


app = CustomFlask(__name__)
power_list = []
redis_client = FlaskRedis(app)

data = {
    'pi4': redis_client.get('pi4'),
}

def get_addr_weather():
    # 彩云天气api
    headers = {
        'Host': 'api.caiyunapp.com',
        'Accept': 'application/json',
        'os-version': '13.3',
        # 'device-id': 'D1118295-F2CE-4685-8F9C-6CEF5D3B9857',
        'app-name': 'weather',
        'User-Agent': 'ColorfulClouds/6.0.2 (iPhone; iOS 13.3; Scale/3.00)',
        'os-type': 'ios',
        'Accept-Language': 'zh-Hans-JP;q=1, en-JP;q=0.9, zh-Hant-TW;q=0.8, ja-JP;q=0.7, en-US;q=0.6',
        'app-version': '6.0.2',
    }

    params = (
        ('lang', 'zh_CN'),
        ('dailystart', '-1'),
        ('hourlysteps', '384'),
        ('dailysteps', '16'),
        ('alert', 'true'),
    )

    response = requests.get(f'https://api.caiyunapp.com/v2.5/UR8ASaplvIwavDfR/<>/weather',
                            headers=headers,
                            params=params).json()
    return response['result']


def power():
    global now, package_list
    data = copy.deepcopy(package_list)
    now_t = time.time()
    seconds = now_t - now
    now = now_t
    for (index, package) in enumerate(package_list):
        package_energy = int(os.popen(f"cat {package['dir']}").read().replace('\n', ''))
        data[index]['used'] = (package_energy - package['energy']) / 1000000.0 / seconds
        package['energy'] = package_energy
        for (i, vo) in enumerate(package['children']):
            energy = int(os.popen(f"cat {vo['dir']}").read().replace('\n', ''))
            data[index]['children'][i]['used'] = (energy - vo['energy']) / 1000000.0 / seconds
            vo['energy'] = energy
    return data


def gpu():
    gpu_info = etree_to_dict(ET.XML(os.popen('nvidia-smi -x -q').read()))['nvidia_smi_log']['gpu']
    gpu_fan = gpu_info['fan_speed']
    gpu_freq = gpu_info['clocks']['mem_clock']
    gpu_temp = gpu_info['temperature']['gpu_temp']
    # gpu_name = gpu_info['product_name']
    gpu_memory_total = gpu_info['fb_memory_usage']['total']
    gpu_memory_free = gpu_info['fb_memory_usage']['free']
    gpu_memory_usage = gpu_info['fb_memory_usage']['used']
    gpu_power_usage = gpu_info['power_readings']['power_draw']
    gpu_power_total = gpu_info['power_readings']['max_power_limit']
    process_list = gpu_info['processes']['process_info']
    gpu_process_list = []
    for process in process_list:
        try:
            name = process['process_name'][process['process_name'].rfind('/') + 1:]
            gpu_process_list.append({
                'pid': process['pid'],
                'name': name,
                'memory': int(process['used_memory'][0:process['used_memory'].find(' ')])
            })
        except:
            continue
    data = {
        'name': gpu_name,
        'freq': gpu_freq,
        'fan': gpu_fan,
        'temp': gpu_temp,
        'power': {
            'usage': gpu_power_usage.replace(' W', ''),
            'total': gpu_power_total.replace(' W', ''),
        },
        'memory': {
            'usage': gpu_memory_usage.replace(' MiB', ''),
            'total': gpu_memory_total.replace(' MiB', ''),
        },
        'process': gpu_process_list[0:5]

    }
    return data


def cpu():
    cpu_freq_info = os.popen("cpupower frequency-info").read()
    try:
        cpu_current_freq = re.search(r'current CPU frequency: (.*) GHz', cpu_freq_info).group(1)
    except AttributeError:
        cpu_current_freq = 0
    cpu_max_freq = re.search(r'and (.*) GHz', cpu_freq_info).group(1)
    cpu_full_freq_info = os.popen('grep \"cpu MHz\" /proc/cpuinfo').read()
    cpu_full_freq_info_arr = cpu_full_freq_info.split('\n')
    cpu_full_freq_info_arr = cpu_full_freq_info_arr[0:len(cpu_full_freq_info_arr) - 1]
    cpu_full_freq = [re.search(r'[0-9]*\.[0-9]*', x).group(0) for x in cpu_full_freq_info_arr]
    top_info = os.popen('top -bn1').read()
    cpu_usage = re.search(r'%Cpu.*:(.*) us', top_info).group(1).replace(' ', '')
    memory_info = re.search(r'Mi B Mem : .*', top_info).group(0)
    memory_total = re.search(r'MiB Mem : (.*) total', memory_info).group(1).replace(' ', '')
    memory_free = re.search(r'total,(.*) free,', memory_info).group(1).replace(' ', '')
    memory_used = re.search(r'free,(.*) used,', memory_info).group(1).replace(' ', '')
    memory_buff = re.search(r'used,(.*) buff', memory_info).group(1).replace(' ', '')

    voltage = []
    for i in range(len(cpu_full_freq)):
        fd = os.open(f'/dev/cpu/{i}/msr', os.O_RDONLY)
        data = int.from_bytes(os.pread(fd, 8, 0x198), byteorder='little')
        os.close(fd)
        data >>= 32
        data &= (1 << 16) - 1
        voltage.append(data / 8192)

    data = {
        'cpu': {
            'name': cpu_name,
            'max_freq': cpu_max_freq,
            'current_freq': cpu_current_freq,
            'full_freq': cpu_full_freq,
            'usage': cpu_usage,
            'voltage': voltage,
        },
        'memory': {
            'total': memory_total,
            'free': memory_free,
            'used': memory_used,
            'buff': memory_buff
        }
    }
    return data


def sensors():
    sensors_info = json.loads(os.popen('sensors -j').read())
    data = {}
    for key, value in sensors_info.items():
        if 'dell' in key:
            data['fan'] = {
                'fan1': value['fan1']['fan1_input'],
                'fan2': value['fan2']['fan2_input'],
                'fan3': value['fan3']['fan3_input'],
            }
        elif 'wifi' in key:
            data['wifi'] = 0
            for k, v in value.items():
                if 'temp' in k:
                    for vo in v.keys():
                        if 'temp' in k:
                            data['wifi'] = v[vo]
        elif 'core' in key:
            data['cpu'] = {'main': 0, 'full': []}
            for k, v in value.items():
                if 'Package' in k:
                    for vo in v.keys():
                        if 'input' in vo:
                            data['cpu']['main'] = v[vo]
                if 'Core ' in k:
                    for vo in v.keys():
                        if 'input' in vo:
                            data['cpu']['full'].append(v[vo])
    return data


def sound():
    sound_info = os.popen('amixer  -R').read()
    return re.findall(r'\d+%', sound_info)


def network():
    def parse_speed(speed):
        if speed < 1000:
            return str(round(speed, 1)) + 'KB/s'
        else:
            return str(round(speed / 1000, 1)) + 'MB/s'

    network_info = os.popen('ifstat').read()
    eno1 = re.search(r'eno1 .*', network_info).group(0)
    eno1_speed = re.findall(r'\d+', eno1)
    eno1_download_speed = int(eno1_speed[5]) / 1000
    eno1_upload_speed = int(eno1_speed[7]) / 1000
    return {
        'eno': {
            'upload': parse_speed(eno1_upload_speed),
            'download': parse_speed(eno1_download_speed),
        }
    }
    wifi = re.search(r'wlp3s0 .*', network_info).group(0)
    wifi_speed = re.findall(r'\d+', wifi)
    wifi_download_speed = int(wifi_speed[6]) / 1000
    wifi_upload_speed = int(wifi_speed[8]) / 1000
    data = {
        'eno': {
            'upload': parse_speed(eno1_upload_speed),
            'download': parse_speed(eno1_download_speed),
        },
        'wifi': {
            'upload': parse_speed(wifi_upload_speed),
            'download': parse_speed(wifi_download_speed),
        }
    }
    return data


def disk():
    disk_info = os.popen('df').read()
    ssd_info = re.search('/dev/nvme.*', disk_info).group(0)
    ssd_info_list = [round(int(x) / 1024 / 1024, 2) for x in re.findall(r'\d+', ssd_info)]
    sda_info = re.search('/dev/sda5.*', disk_info).group(0)
    sda_info_list = [round(int(x) / 1024 / 1024, 2) for x in re.findall(r'[-+]?\d*\.\d+|\d+', sda_info)]
    data = {
        'ssd': {
            'name': ssd_info[ssd_info.rfind(' ') + 1:],
            'device': 'Toshiba BG3',
            'total': ssd_info_list[3],
            'used': ssd_info_list[4],
            'free': ssd_info_list[5],
            'percent': re.search(r'\d+%', ssd_info).group(0).replace('%', '')
        },
        'hhd': {
            'name': sda_info[sda_info.rfind(' ') + 1:],
            'device': 'BarraCuda 3.5',
            'total': sda_info_list[1],
            'used': sda_info_list[2],
            'free': sda_info_list[3],
            'percent': re.search(r'\d+%', sda_info).group(0).replace('%', '')
        }
    }
    return data


@app.route('/api')
def api():
    data = cpu()
    data['weather'] = get_addr_weather()
    data['sensors'] = sensors()
    data['gpu'] = gpu()
    data['disk'] = disk()
    data['network'] = network()
    data['sound'] = sound()
    # data['time'] = [datetime.now().strftime('%Y/%m/%d'), datetime.now().strftime('%H:%M:%S')]
    data['power'] = power()
    return data


@app.route('/ip')
def get_ip():
    client = request.args.get('client')
    if client == 'pi4':
        ip = request.remote_addr
        redis_client.set('pi4',ip)
        data['pi4'] = ip
    return data


@app.route('/')
def main():
    init_power()
    return render_template('ipadmini2.html', )


if __name__ == '__main__':
    init_power()
    gzip = Gzip(app)
    app.run(debug=True, host="0.0.0.0")
