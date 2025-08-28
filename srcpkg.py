#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
srcpkg.py — gerenciador source-based (protótipo completo)
Recursos: patches, deps recursivas, DESTDIR + pacote .tar.xz, strip opcional,
hooks pós-install/remove (globais e por receita), órfãos, revdep (--rebuild),
search/sync/upgrade, logs e spinner simples.
"""
from __future__ import annotations
import argparse, contextlib, dataclasses, hashlib, json, os, re, shutil, stat, subprocess, sys, tarfile, threading, time, zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set

# ==== UI/cores ====
RESET="\033[0m"; BOLD="\033[1m"; DIM="\033[2m"
BLACK="\033[30m"; RED="\033[31m"; GREEN="\033[32m"; YELLOW="\033[33m"; BLUE="\033[34m"; MAG="\033[35m"; CYAN="\033[36m"; WHITE="\033[37m"
def c(t, col): return f"{col}{t}{RESET}"
def log_info(m): print(c("[INFO] ", BLUE)+m)
def log_ok(m): print(c("[ OK ] ", GREEN)+m)
def log_warn(m): print(c("[WARN] ", YELLOW)+m)
def log_err(m): print(c("[ERR ] ", RED)+m)

# ==== Caminhos ====
HOME = Path.home()
SRCPKG_ROOT = Path(os.environ.get("SRCPKG_ROOT", HOME/".local/share/srcpkg"))
SRCPKG_DB   = SRCPKG_ROOT/"db"
SRCPKG_BUILD= Path(os.environ.get("SRCPKG_BUILD", SRCPKG_ROOT/"build"))
SRCPKG_PKGS = Path(os.environ.get("SRCPKG_PKGS", SRCPKG_ROOT/"packages"))
SRCPKG_SRC  = Path(os.environ.get("SRCPKG_SRC", SRCPKG_ROOT/"sources"))
SRCPKG_LOGS = SRCPKG_ROOT/"logs"
HOOKS_DIR   = SRCPKG_ROOT/"hooks"
REPO        = Path(os.environ.get("REPO", HOME/"srcpkg-repo"))
for d in [SRCPKG_ROOT, SRCPKG_DB, SRCPKG_BUILD, SRCPKG_PKGS, SRCPKG_SRC, SRCPKG_LOGS, HOOKS_DIR/"post-install.d", HOOKS_DIR/"post-remove.d"]:
    d.mkdir(parents=True, exist_ok=True)

# ==== Utilitários ====
def which(cmd:str)->Optional[str]: return shutil.which(cmd)
def compute_sha256(path:Path)->str:
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1024*1024), b''): h.update(chunk)
    return h.hexdigest()

class Spinner:
    def __init__(self, text=""): self.text=text; self.stop=False; self.t=None
    def _run(self):
        frames="|/-\\"
        i=0
        while not self.stop:
            print(f"\r{CYAN}{frames[i%len(frames)]} {self.text}{RESET}", end="", flush=True)
            i+=1; time.sleep(0.08)
        print("\r"+(" "*(len(self.text)+4))+"\r", end="", flush=True)
    def __enter__(self): self.stop=False; self.t=threading.Thread(target=self._run, daemon=True); self.t.start()
    def __exit__(self, *exc): self.stop=True; self.t.join(timeout=0.2)

def run(cmd:List[str]|str, cwd:Optional[Path]=None, env:Optional[Dict[str,str]]=None, check=True, logfile:Optional[Path]=None):
    if isinstance(cmd, list): show=" ".join(cmd); shell=False; args=cmd
    else: show=cmd; shell=True; args=cmd
    log_info(f"$ {show}")
    # stream stdout/stderr para terminal + log
    with subprocess.Popen(args, cwd=str(cwd) if cwd else None, env=env, shell=shell, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1) as p:
        if logfile: f= open(logfile, "a", encoding="utf-8")
        else: f=None
        for line in p.stdout:
            print(line, end="")
            if f: f.write(line)
        ret=p.wait()
        if f: f.close()
        if check and ret!=0:
            raise subprocess.CalledProcessError(ret, show)

# ==== Modelo de dados ====
@dataclasses.dataclass
class SourceSpec: url:Optional[str]=None; sha256:Optional[str]=None; type:str="archive"
@dataclasses.dataclass
class PatchSpec: url:Optional[str]=None; file:Optional[str]=None; sha256:Optional[str]=None; strip:int=1
@dataclasses.dataclass
class BuildSpec:
    env:Dict[str,str]=dataclasses.field(default_factory=dict)
    prepare:List[str]=dataclasses.field(default_factory=list)
    compile:List[str]=dataclasses.field(default_factory=list)
    install:List[str]=dataclasses.field(default_factory=list)
@dataclasses.dataclass
class PackageMeta:
    name:str; version:str; category:str="extras"; homepage:Optional[str]=None
    source:SourceSpec=dataclasses.field(default_factory=SourceSpec)
    git:Dict[str,str]=dataclasses.field(default_factory=dict)
    patches:List[PatchSpec]=dataclasses.field(default_factory=list)
    dependencies:List[str]=dataclasses.field(default_factory=list)
    build:BuildSpec=dataclasses.field(default_factory=BuildSpec)
    package:Dict[str,object]=dataclasses.field(default_factory=dict) # ex: {"strip": true}
    hooks:Dict[str,List[str]]=dataclasses.field(default_factory=dict) # ex: {"post_install":[...],"post_remove":[...]}

    @staticmethod
    def from_json(path:Path)->"PackageMeta":
        data=json.loads(Path(path).read_text())
        src=data.get("source",{}); source=SourceSpec(url=src.get("url"), sha256=src.get("sha256"), type=src.get("type","archive"))
        patches=[PatchSpec(url=p.get("url"), file=p.get("file"), sha256=p.get("sha256"), strip=int(p.get("strip",1))) for p in data.get("patches",[])]
        b=data.get("build",{}); build=BuildSpec(env=b.get("env",{}), prepare=b.get("prepare",[]), compile=b.get("compile",[]), install=b.get("install",[]))
        return PackageMeta(name=data["name"], version=data["version"], category=data.get("category","extras"), homepage=data.get("homepage"),
                           source=source, git=data.get("git",{}), patches=patches, dependencies=data.get("dependencies",[]),
                           build=build, package=data.get("package",{}), hooks=data.get("hooks",{}))

@dataclasses.dataclass
class InstalledPkg:
    name:str; version:str; files:List[str]; depends:List[str]; recipe:Dict[str,object]
    def to_json(self)->str: return json.dumps(dataclasses.asdict(self), indent=2, ensure_ascii=False)
    @staticmethod
    def load(name:str)->Optional["InstalledPkg"]:
        p=SRCPKG_DB/f"{name}.json"
        if not p.exists(): return None
        d=json.loads(p.read_text()); return InstalledPkg(name=d["name"], version=d["version"], files=d.get("files",[]), depends=d.get("depends",[]), recipe=d.get("recipe",{}))
    def save(self): (SRCPKG_DB/f"{self.name}.json").write_text(self.to_json())

# ==== Núcleo ====
class SrcPkg:
    def __init__(self):
        self.current_log: Optional[Path]=None

    # ----- Repositório -----
    def repo_subdirs(self)->List[Path]: return [REPO/s for s in ["base","x11","extras","desktop"]]
    def scan_repo_recipes(self)->Dict[str,Path]:
        found={}
        for d in self.repo_subdirs():
            if not d.exists(): continue
            for path in d.rglob("*.json"):
                try:
                    data=json.loads(path.read_text()); name=data.get("name")
                    if name: found[name]=path
                except Exception: continue
        return found

    # ----- Download/extração -----
    def download(self, src:SourceSpec, workdir:Path)->Path:
        workdir.mkdir(parents=True, exist_ok=True)
        if src.type=="git":
            repo=src.url
            if not repo: raise ValueError("source.url obrigatório para git")
            dest=workdir/"src"
            if dest.exists(): run(["git","fetch","--all"], cwd=dest, logfile=self.current_log)
            else: run(["git","clone", repo, str(dest)], logfile=self.current_log)
            return dest
        else:
            if not src.url: raise ValueError("source.url obrigatório para archive")
            out=SRCPKG_SRC/src.url.split("/")[-1]
            if not out.exists():
                cmd=None
                if which("wget"): cmd=["wget","-O",str(out), src.url]
                elif which("curl"): cmd=["curl","-L","-o",str(out), src.url]
                else: raise RuntimeError("Necessário wget ou curl")
                with Spinner("baixando source"): run(cmd, logfile=self.current_log)
            if src.sha256:
                got=compute_sha256(out)
                if got!=src.sha256: raise RuntimeError(f"sha256 incorreto: {got}")
                log_ok("sha256 verificado")
            return out

    def extract(self, archive_or_dir:Path, workdir:Path)->Path:
        if archive_or_dir.is_dir(): return archive_or_dir
        dest=workdir/"src"
        if dest.exists(): shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)
        name=archive_or_dir.name
        if name.endswith((".tar.gz",".tgz",".tar.bz2",".tbz2",".tar.xz",".txz",".tar.zst")):
            if name.endswith(".tar.zst"):
                run(["tar","--use-compress-program=unzstd","-xf",str(archive_or_dir),"-C",str(dest)], logfile=self.current_log)
            else:
                # auto: tarfile abre gz/bz2/xz
                with Spinner("extraindo"): 
                    with tarfile.open(archive_or_dir, "r:*") as tf: tf.extractall(dest)
        elif name.endswith(".zip"):
            with Spinner("extraindo zip"):
                with zipfile.ZipFile(archive_or_dir,'r') as z: z.extractall(dest)
        elif name.endswith((".7z",".7zip")):
            if which("7z"): run(["7z","x",str(archive_or_dir), f"-o{dest}"], logfile=self.current_log)
            else: raise RuntimeError("7z necessário para .7z")
        else:
            raise RuntimeError(f"Formato não suportado: {name}")
        entries=list(dest.iterdir())
        return entries[0] if len(entries)==1 and entries[0].is_dir() else dest

    def fetch_patch(self, p:PatchSpec, patch_dir:Path)->Path:
        patch_dir.mkdir(parents=True, exist_ok=True)
        if p.file:
            path=Path(p.file)
            if not path.is_absolute(): path=patch_dir/p.file
        else:
            if not p.url: raise ValueError("Patch sem 'file' nem 'url'")
            path=patch_dir/p.url.split('/')[-1]
            if not path.exists():
                if which("wget"): run(["wget","-O",str(path), p.url], logfile=self.current_log)
                elif which("curl"): run(["curl","-L","-o",str(path), p.url], logfile=self.current_log)
                else: raise RuntimeError("wget/curl necessário" )
        if p.sha256:
            got=compute_sha256(path)
            if got!=p.sha256: raise RuntimeError("sha256 incorreto para patch")
        return path

    def apply_patches(self, meta:PackageMeta, srcdir:Path, patch_dir:Path):
        if not meta.patches: return
        for p in meta.patches:
            patch_path=self.fetch_patch(p, patch_dir)
            strip=str(int(p.strip))
            log_info(f"Aplicando patch {patch_path.name} -p{strip}")
            run(f"patch -p{strip} -t -N -r - -i {patch_path}", cwd=srcdir, logfile=self.current_log)

    # ----- Build/package/install -----
    def _expand_env(self, env:Dict[str,str], extra:Dict[str,str])->Dict[str,str]:
        base=os.environ.copy(); base.update(env or {}); base.update(extra or {})
        for k,v in list(base.items()): base[k]=os.path.expandvars(str(v))
        return base

    def run_script(self, lines:List[str], cwd:Path, env:Dict[str,str]):
        if not lines: return
        shell=os.environ.get('SHELL','/bin/sh'); script="\n".join(lines)
        run([shell,"-exc", script], cwd=cwd, env=env, logfile=self.current_log)

    def _strip_files(self, root:Path):
        if not which('strip'): return
        for p in root.rglob('*'):
            if not p.is_file(): continue
            try:
                st=os.stat(p)
                if (st.st_mode & stat.S_IXUSR) or p.suffix=='.so' or '.so.' in p.name:
                    run(['strip','--strip-unneeded', str(p)], check=False, logfile=self.current_log)
            except Exception: pass

    def package(self, meta:PackageMeta, destdir:Path, outdir:Path)->Path:
        outdir.mkdir(parents=True, exist_ok=True)
        if meta.package.get('strip', False): self._strip_files(destdir)
        pkgname=f"{meta.name}-{meta.version}-1.tar.xz"
        out=outdir/pkgname
        with tarfile.open(out, mode='w:xz') as tf: tf.add(destdir, arcname=".")
        log_ok(f"Pacote gerado: {out}"); return out

    def _copy_tree(self, src:Path, dst:Path)->List[Path]:
        files=[]
        for root, dirs, filenames in os.walk(src):
            rel=os.path.relpath(root, src)
            for d in dirs: (Path(dst)/rel/d).mkdir(parents=True, exist_ok=True)
            for f in filenames:
                s=Path(root)/f; t=Path(dst)/rel/f; t.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(s,t); files.append(t)
        return files

    def _run_hooks(self, hooks:List[str], cwd:Optional[Path]=None):
        if not hooks: return
        env=os.environ.copy()
        for line in hooks:
            run(line, cwd=cwd, env=env, check=False, logfile=self.current_log)

    def _run_global_hooks(self, which_dir:str, pkgname:str):
        hookdir=HOOKS_DIR/f"{which_dir}.d"
        if not hookdir.exists(): return
        for hook in sorted(hookdir.glob('*')):
            if os.access(hook, os.X_OK):
                run([str(hook), pkgname], check=False, logfile=self.current_log)

    def install_package_dir(self, meta:PackageMeta, destdir:Path)->InstalledPkg:
        if os.geteuid()!=0:
            log_err('Requer root para instalar. Use sudo.'); sys.exit(1)
        files=[str(p) for p in self._copy_tree(destdir, Path('/'))]
        pkg=InstalledPkg(name=meta.name, version=meta.version, files=files, depends=meta.dependencies, recipe=self._meta_to_dict(meta))
        pkg.save()
        self._run_hooks(meta.hooks.get("post_install",[]))
        self._run_global_hooks("post-install", meta.name)
        return pkg

    def _meta_to_dict(self, meta:PackageMeta)->Dict[str,object]:
        # para persistir receita no DB (revdep --rebuild/upgrade)
        return json.loads(json.dumps({
            "name": meta.name, "version": meta.version, "category": meta.category, "homepage": meta.homepage,
            "source": dataclasses.asdict(meta.source), "git": meta.git,
            "patches": [dataclasses.asdict(p) for p in meta.patches],
            "dependencies": meta.dependencies, "build": dataclasses.asdict(meta.build),
            "package": meta.package, "hooks": meta.hooks
        }))

    def build(self, meta:PackageMeta, work:Path, only_build:bool=True)->Tuple[Path,Path]:
        self.current_log = SRCPKG_LOGS/f"{meta.name}.log"
        self.current_log.touch(exist_ok=True)
        src_path=self.download(meta.source if meta.source.type!='git' else SourceSpec(url=meta.git.get('repo'), type='git'), work)
        srcdir=self.extract(src_path, work)
        self.apply_patches(meta, srcdir, work/"patches")
        destdir=work/"destdir"; destdir.mkdir(parents=True, exist_ok=True)
        env=self._expand_env(meta.build.env, {"DESTDIR": str(destdir)})
        self.run_script(meta.build.prepare, srcdir, env)
        self.run_script(meta.build.compile, srcdir, env)
        if not only_build: self.run_script(meta.build.install, srcdir, env)
        return srcdir, destdir

    def install_from_meta(self, meta:PackageMeta):
        work=SRCPKG_BUILD/f"{meta.name}-{meta.version}"
        if work.exists(): shutil.rmtree(work)
        work.mkdir(parents=True, exist_ok=True)
        _, destdir=self.build(meta, work, only_build=False)
        pkgpath=self.package(meta, destdir, SRCPKG_PKGS)
        self.install_package_dir(meta, destdir)
        log_ok(f"Instalado {meta.name}-{meta.version}  (log: {self.current_log})")

    def build_only(self, meta:PackageMeta, work:Path):
        self.build(meta, work, only_build=True); log_ok("Build concluído (sem instalar)")

    def package_only(self, meta:PackageMeta, work:Path):
        _, destdir=self.build(meta, work, only_build=False); self.package(meta, destdir, SRCPKG_PKGS)

    # ----- DB/listas -----
    def list_installed(self)->List[InstalledPkg]:
        res=[]
        for p in SRCPKG_DB.glob('*.json'):
            try:
                d=json.loads(p.read_text()); res.append(InstalledPkg(name=d['name'], version=d['version'], files=d.get('files',[]), depends=d.get('depends',[]), recipe=d.get('recipe',{})))
            except Exception: continue
        return sorted(res, key=lambda x: x.name)

    def remove(self, name:str):
        pkg=InstalledPkg.load(name)
        if not pkg: log_warn(f"{name} não está instalado"); return
        for f in sorted(pkg.files, key=lambda x: len(x), reverse=True):
            p=Path(f)
            try:
                if p.is_file() or p.is_symlink(): p.unlink(missing_ok=True)
                with contextlib.suppress(Exception):
                    d=p.parent
                    while d!=Path('/'):
                        if not any(d.iterdir()): d.rmdir(); d=d.parent
                        else: break
            except Exception as e: log_warn(f"Falha removendo {f}: {e}")
        self._run_hooks(pkg.recipe.get("hooks",{}).get("post_remove",[]))
        self._run_global_hooks("post-remove", name)
        with contextlib.suppress(Exception): (SRCPKG_DB/f"{name}.json").unlink()
        log_ok(f"Removido {name}")

    # ----- Orfãos -----
    def orphans(self)->List[str]:
        installed=self.list_installed(); names={p.name for p in installed}; required:set[str]=set()
        for p in installed:
            for d in p.depends: required.add(d)
        return sorted(list(names - required))

    # ----- Search/Info -----
    def search(self, term:str):
        term=term.lower()
        repo=self.scan_repo_recipes()
        installed={p.name for p in self.list_installed()}
        print(c("Disponíveis no repositório:", BOLD))
        for name in sorted(repo.keys()):
            if term in name.lower():
                mark=c(" [instalado]", GREEN) if name in installed else ""
                print(f"  - {name}{mark}")
        print(c("\nInstalados:", BOLD))
        for p in self.list_installed():
            if term in p.name.lower(): print(f"  - {p.name} {p.version}")

    def info(self, name_or_recipe:str):
        path=Path(name_or_recipe); meta=None
        if path.exists(): meta=PackageMeta.from_json(path)
        else:
            r=self.scan_repo_recipes().get(name_or_recipe)
            if r: meta=PackageMeta.from_json(r)
        if meta:
            print(c(f"{meta.name} {meta.version}", BOLD)); print(f"Categoria: {meta.category}")
            if meta.homepage: print(f"Homepage: {meta.homepage}")
            if meta.dependencies: print("Dependências:", ", ".join(meta.dependencies))
        inst=InstalledPkg.load(meta.name if meta else name_or_recipe)
        if inst: print(c("\nEstado: INSTALADO", GREEN)); print(f"Arquivos: {len(inst.files)}"); print(f"Versão instalada: {inst.version}")
        else: print(c("\nEstado: não instalado", YELLOW))

    # ----- Sync/Upgrade -----
    def sync_repo(self):
        for d in self.repo_subdirs(): d.mkdir(parents=True, exist_ok=True)
        if which('git') and (REPO/'.git').exists():
            run(['git','-C',str(REPO),'pull','--rebase'], check=False)
            run(['git','-C',str(REPO),'push'], check=False)
            log_ok('Repositório sincronizado')
        else: log_warn('REPO não é um repositório git; apenas diretórios garantidos.')

    @staticmethod
    def _verkey(v:str)->Tuple:
        parts=re.split(r"[^0-9A-Za-z]+", v); key=[]
        for p in parts:
            if p.isdigit(): key.append((0,int(p)))
            else: key.append((1,p))
        return tuple(key)

    def upgrade(self, meta:PackageMeta):
        inst=InstalledPkg.load(meta.name)
        if not inst: log_warn(f"{meta.name} não está instalado; use 'install'"); return
        if self._verkey(meta.version) <= self._verkey(inst.version):
            log_info(f"Instalado ({inst.version}) >= receita ({meta.version}); nada a fazer"); return
        log_info(f"Atualizando {meta.name} {inst.version} -> {meta.version}")
        self.install_from_meta(meta)

    # ----- Dependências -----
    def _resolve_deps_install_first(self, meta:PackageMeta, visited:Set[str]|None=None):
        if visited is None: visited=set()
        if meta.name in visited: return
        visited.add(meta.name)
        repo=self.scan_repo_recipes()
        for dep in meta.dependencies:
            if InstalledPkg.load(dep): continue
            rpath = repo.get(dep) or Path(f"{dep}.json")
            if not rpath.exists(): raise RuntimeError(f"Receita de dependência não encontrada: {dep}")
            dmeta=PackageMeta.from_json(rpath)
            self._resolve_deps_install_first(dmeta, visited)
            self.install_from_meta(dmeta)

    # ----- Revdep -----
    def _lib_providers(self)->Dict[str, Set[str]]:
        providers={}
        for pkg in self.list_installed():
            for f in pkg.files:
                name=os.path.basename(f)
                if name.startswith("lib") and ".so" in name:
                    providers.setdefault(name, set()).add(pkg.name)
        return providers

    def revdep(self, rebuild:bool=False):
        missing_by_pkg: Dict[str, Set[str]]={}
        providers=self._lib_providers()
        for pkg in self.list_installed():
            miss=set()
            for f in pkg.files:
                p=Path(f)
                if not p.exists(): continue
                # só binários potencialmente dinâmicos
                try:
                    mode=os.stat(p).st_mode
                    if not (p.is_file() and ((mode & stat.S_IXUSR) or ".so" in p.name)): continue
                except Exception: continue
                if not which("ldd"): continue
                try:
                    out=subprocess.run(["ldd", str(p)], text=True, capture_output=True, check=False).stdout
                except Exception:
                    continue
                for line in out.splitlines():
                    if "not found" in line:
                        m=re.search(r"(\S+)\s+=>\s+not found", line)
                        lib = m.group(1) if m else line.strip()
                        miss.add(lib)
            if miss:
                missing_by_pkg[pkg.name]=miss

        if not missing_by_pkg:
            log_ok("Nenhuma dependência quebrada encontrada.")
            return

        print(c("Pacotes com dependências ausentes:", BOLD))
        for name, libs in missing_by_pkg.items():
            prov=[ (lib, list(providers.get(os.path.basename(lib), []))) for lib in libs ]
            print(f" - {name}: faltando {', '.join(libs)}")
            for lib, pkgs in prov:
                if pkgs:
                    print(f"     ↳ possível provedor: {', '.join(pkgs)}")

        if rebuild:
            log_info("Rebuild automático ativado (--rebuild)")
            for name in missing_by_pkg.keys():
                inst=InstalledPkg.load(name)
                if not inst or not inst.recipe:
                    log_warn(f"Sem receita salva para {name}; pulando")
                    continue
                meta=self._dict_to_meta(inst.recipe)
                self._resolve_deps_install_first(meta)
                self.install_from_meta(meta)

    def _dict_to_meta(self, d:Dict[str,object])->PackageMeta:
        # reconstrói PackageMeta a partir do dict salvo no DB
        src=SourceSpec(**d.get("source",{}))
        patches=[PatchSpec(**pp) for pp in d.get("patches",[])]
        b=d.get("build",{}); build=BuildSpec(**{k:b.get(k,[]) if k in ("prepare","compile","install") else b.get(k,{}) for k in ["env","prepare","compile","install"]})
        return PackageMeta(name=d["name"], version=d["version"], category=d.get("category","extras"), homepage=d.get("homepage"),
                           source=src, git=d.get("git",{}), patches=patches, dependencies=d.get("dependencies",[]),
                           build=build, package=d.get("package",{}), hooks=d.get("hooks",{}))

# ==== CLI ====
def load_meta_from_arg(arg:str)->PackageMeta:
    path=Path(arg)
    if path.exists(): return PackageMeta.from_json(path)
    # se for nome, procura no REPO
    repo=SrcPkg().scan_repo_recipes()
    r=repo.get(arg) or Path(f"{arg}.json")
    if not r.exists(): raise SystemExit(f"Receita não encontrada: {arg}")
    return PackageMeta.from_json(r)

def cmd_build(a): m=SrcPkg(); meta=load_meta_from_arg(a.recipe); work=SRCPKG_BUILD/f"{meta.name}-{meta.version}"
work.exists() and shutil.rmtree(work); work.mkdir(parents=True, exist_ok=True); m.build_only(meta, work)

def cmd_package(a): m=SrcPkg(); meta=load_meta_from_arg(a.recipe); work=SRCPKG_BUILD/f"{meta.name}-{meta.version}"
work.exists() and shutil.rmtree(work); work.mkdir(parents=True, exist_ok=True); m.package_only(meta, work)

def cmd_install(a): m=SrcPkg(); meta=load_meta_from_arg(a.recipe); m._resolve_deps_install_first(meta); m.install_from_meta(meta)

def cmd_remove(a): m=SrcPkg(); m.remove(a.name)

def cmd_list(a): m=SrcPkg(); [print(f"{p.name} {p.version}") for p in m.list_installed()]

def cmd_info(a): m=SrcPkg(); m.info(a.name_or_recipe)

def cmd_orphans(a): m=SrcPkg(); orfs=m.orphans(); print("Órfãos:"); [print(f"  - {n}") for n in orfs]
if 'a' in locals() and hasattr(a, 'remove') and a.remove:
    for n in orfs: m.remove(n)

def cmd_search(a): m=SrcPkg(); m.search(a.term)

def cmd_sync(a): m=SrcPkg(); m.sync_repo()

def cmd_upgrade(a): m=SrcPkg(); meta=load_meta_from_arg(a.recipe); m.upgrade(meta)

def cmd_revdep(a): m=SrcPkg(); m.revdep(rebuild=a.rebuild)

def make_parser()->argparse.ArgumentParser:
    p=argparse.ArgumentParser(description="srcpkg — gerenciador source-based")
    sub=p.add_subparsers(dest='cmd', required=True)
    sp=sub.add_parser('build'); sp.add_argument('recipe'); sp.set_defaults(func=cmd_build)
    sp=sub.add_parser('package'); sp.add_argument('recipe'); sp.set_defaults(func=cmd_package)
    sp=sub.add_parser('install'); sp.add_argument('recipe'); sp.set_defaults(func=cmd_install)
    sp=sub.add_parser('remove'); sp.add_argument('name'); sp.set_defaults(func=cmd_remove)
    sp=sub.add_parser('list'); sp.set_defaults(func=cmd_list)
    sp=sub.add_parser('info'); sp.add_argument('name_or_recipe'); sp.set_defaults(func=cmd_info)
    sp=sub.add_parser('orphans'); sp.add_argument('--remove', action='store_true'); sp.set_defaults(func=cmd_orphans)
    sp=sub.add_parser('search'); sp.add_argument('term'); sp.set_defaults(func=cmd_search)
    sp=sub.add_parser('sync'); sp.set_defaults(func=cmd_sync)
    sp=sub.add_parser('upgrade'); sp.add_argument('recipe'); sp.set_defaults(func=cmd_upgrade)
    sp=sub.add_parser('revdep'); sp.add_argument('--rebuild', action='store_true'); sp.set_defaults(func=cmd_revdep)
    return p

def main():
    parser=make_parser(); args=parser.parse_args()
    try:
        args.func(args)
    except subprocess.CalledProcessError as e:
        log_err(f"Comando falhou: {e.returncode}"); sys.exit(e.returncode)
    except KeyboardInterrupt:
        log_warn('Interrompido pelo usuário'); sys.exit(130)
    except Exception as e:
        log_err(str(e)); sys.exit(1)

if __name__=='__main__': main()
