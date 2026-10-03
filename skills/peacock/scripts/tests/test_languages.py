"""
One parser test per supported language — nineteen of them, plus the fallback.

    python3 -m unittest tests.test_languages -v

Each case is a small file written in the idiom of a large, popular repository
in that language (named in `mirrors`, and the same repository the corpus suite
in `test_corpus.py` runs the parser over for real). The snippets are written
here rather than copied from upstream so this repository carries no other
project's licence, but every construct in them is one that actually appears in
the repository named.

Every case deliberately contains the four shapes that have broken this parser
before, because a language "works" only if it survives all four:

  1. a declaration whose signature wraps onto a second line,
  2. a declaration-shaped line inside a comment,
  3. a declaration-shaped line inside a string literal,
  4. control flow — `if (x) {`, `catch (e) {` — that has a function's shape.

Cases 2 and 3 are not pedantry. A Javadoc `@see Foo#refresh()` once produced a
call edge at the highest confidence tier, and a `/*` inside a Java string
literal opened a block comment that swallowed the rest of the file in 107
spring-boot files. Both were found in review, not by a test. Now they are
tested per language.

What the parser gets *wrong* is in `TestKnownLimitations` at the bottom, as
ordinary tests marked `@unittest.expectedFailure`. Each one asserts the
behaviour a correct parser would have, so the list reads as a defect register
rather than as a blessing. unittest reports them as expected failures, and — the
reason for doing it this way — reports an *unexpected success* when one starts
passing, which fails the run until the entry is deleted. A limitation cannot be
fixed and left documented as broken, and cannot be documented and quietly get
worse.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.languages import LANGUAGES, lang_for                  # noqa: E402
from engine.parser import parse_source                            # noqa: E402


class Case:
    """A source file and everything the parser must (and must not) say about it."""

    def __init__(self, language, filename, source, functions=(), classes=(),
                 imports=(), absent=(), mirrors=""):
        self.language = language
        self.filename = filename
        self.source = source
        self.functions = list(functions)
        self.classes = list(classes)
        self.imports = list(imports)
        self.absent = list(absent)      # names that must never be symbols
        self.mirrors = mirrors


PYTHON = Case(
    "python", "queryset.py", mirrors="django/django",
    source='''\
"""Query construction, in the shape django/db/models/query.py uses."""
import copy
from collections import namedtuple
from django.db.models import sql
from django.core.exceptions import EmptyResultSet


class QuerySet:
    """Represent a lazy database lookup for a set of objects."""

    def __init__(self, model=None, query=None, using=None):
        self.model = model
        self._query = query or sql.Query(model)
        self._result_cache = None

    def filter(self, *args, **kwargs):
        # def not_a_function(self): a declaration inside a comment
        return self._filter_or_exclude(False, args, kwargs)

    def _filter_or_exclude(
        self,
        negate,
        args,
        kwargs,
    ):
        if self._result_cache is not None:
            raise TypeError("Cannot filter a query once a slice has been taken.")
        clone = self._chain()
        return clone

    async def aget(self, *args, **kwargs):
        return self.get(*args, **kwargs)

    @property
    def db(self):
        return self._db or "default"


def get_related_populators(klass_info, select, db):
    iterators = []
    for rel_klass_info in klass_info["related_klass_infos"]:
        iterators.append(rel_klass_info)
    return iterators


TEMPLATE = """
def fabricated_from_a_docstring(x):
    pass
"""
''',
    functions=["__init__", "filter", "_filter_or_exclude", "aget", "db",
               "get_related_populators"],
    classes=["QuerySet"],
    imports=["copy", "collections", "django.db.models", "django.core.exceptions"],
    absent=["not_a_function", "fabricated_from_a_docstring"],
)

JAVASCRIPT = Case(
    "javascript", "ReactHooks.js", mirrors="facebook/react",
    source='''\
/**
 * Hook dispatch, in the shape packages/react/src/ReactHooks.js uses.
 * @see resolveDispatcher() — a call inside a doc comment, not a declaration.
 */
import ReactCurrentDispatcher from './ReactCurrentDispatcher';
import {REACT_MEMO_TYPE} from 'shared/ReactSymbols';
const invariant = require('shared/invariant');

function resolveDispatcher() {
  const dispatcher = ReactCurrentDispatcher.current;
  return dispatcher;
}

export function useContext(Context) {
  const dispatcher = resolveDispatcher();
  if (dispatcher !== null) {
    return dispatcher.useContext(Context);
  }
  return null;
}

export function useState(
  initialState,
) {
  const dispatcher = resolveDispatcher();
  return dispatcher.useState(initialState);
}

export const useCallback = (callback, deps) => {
  return resolveDispatcher().useCallback(callback, deps);
};

const useDebugValue = async (value, formatter) => value;

export default class Component {
  setState(partial, callback) {
    this.updater.enqueueSetState(this, partial, callback);
  }

  render() {
    // function neverDeclared() {}
    const warning = "function alsoNeverDeclared() {}";
    try {
      return this.props.children;
    } catch (error) {
      return null;
    }
  }
}
''',
    functions=["resolveDispatcher", "useContext", "useState", "useCallback",
               "useDebugValue", "setState", "render"],
    classes=["Component"],
    imports=["./ReactCurrentDispatcher", "shared/ReactSymbols", "shared/invariant"],
    absent=["neverDeclared", "alsoNeverDeclared", "catch", "if"],
)

TYPESCRIPT = Case(
    "typescript", "nest-factory.ts", mirrors="nestjs/nest",
    source='''\
import { Logger } from '../common/services/logger.service';
import { NestApplicationOptions } from '@nestjs/common';
const { NestContainer } = require('./injector/container');

export interface INestApplication {
  init(): Promise<this>;
  listen(port: number | string): Promise<any>;
}

export type HttpsOptions = {
  key: string;
  cert: string;
};

export enum ApplicationScope {
  Default = 0,
  Request = 1,
}

export class NestFactoryStatic {
  private readonly logger = new Logger('NestFactory', { timestamp: true });

  public async create(
    module: any,
    serverOrOptions?: NestApplicationOptions,
  ): Promise<INestApplication> {
    const container = new NestContainer();
    if (serverOrOptions && this.isHttpServer(serverOrOptions)) {
      return this.createInstance(module, container);
    }
    return this.createInstance(module, container);
  }

  private isHttpServer(value: unknown): boolean {
    // private neverDeclared(x: number): void {}
    return typeof value === 'object';
  }

  protected createInstance(module: any, container: unknown): any {
    const message = "public alsoNeverDeclared(): void {}";
    return { module, container, message };
  }
}

export const createApplicationContext = async (module: any): Promise<void> => {
  await new NestFactoryStatic().create(module);
};

export function isFunction(value: any): value is Function {
  return typeof value === 'function';
}
''',
    functions=["create", "isHttpServer", "createInstance",
               "createApplicationContext", "isFunction"],
    classes=["INestApplication", "HttpsOptions", "ApplicationScope",
             "NestFactoryStatic"],
    imports=["../common/services/logger.service", "@nestjs/common",
             "./injector/container"],
    absent=["neverDeclared", "alsoNeverDeclared", "if"],
    # `init(): Promise<this>` and `listen(...)` are interface members with no
    # body. The TS method pattern requires `:` or `{` after the parameter list
    # and gets `:` here, so they *are* found — see test_typescript_interface.
)

JAVA = Case(
    "java", "SpringApplication.java", mirrors="spring-projects/spring-boot",
    source='''\
package org.springframework.boot;

import java.util.Collections;
import java.util.Set;
import static org.springframework.util.Assert.notNull;

/**
 * Application bootstrap, in the shape SpringApplication.java uses.
 *
 * @see ConfigurableApplicationContext#refresh()
 */
public class SpringApplication {

    private static final String SYSTEM_PROPERTY = "java.awt.headless";

    public SpringApplication(Class<?>... primarySources) {
        this.primarySources = new LinkedHashSet<>(Arrays.asList(primarySources));
    }

    public ConfigurableApplicationContext run(String... args) {
        long startTime = System.nanoTime();
        DefaultBootstrapContext bootstrapContext = createBootstrapContext();
        if (args.length > 0) {
            notNull(args, "args must not be null");
        }
        return context;
    }

    private ConfigurableEnvironment prepareEnvironment(
            SpringApplicationRunListeners listeners,
            DefaultBootstrapContext bootstrapContext,
            ApplicationArguments applicationArguments) {
        ConfigurableEnvironment environment = getOrCreateEnvironment();
        return environment;
    }

    protected void load(ApplicationContext context, Object[] sources)
            throws BeanDefinitionStoreException {
        String pattern = "classpath*:org/springframework/**/*.class";
        BeanDefinitionLoader loader = createBeanDefinitionLoader(context);
        loader.load();
    }

    private void handleRunFailure(ConfigurableApplicationContext context,
            Throwable exception) {
        try {
            handleExitCode(context, exception);
        } catch (Exception ex) {
            logger.warn("Unable to close ApplicationContext", ex);
        }
    }
}

interface ApplicationRunner {

    void run(ApplicationArguments args) throws Exception;

    default void before() {
    }
}

abstract class AbstractRunner implements ApplicationRunner {

    protected abstract String name();
}
''',
    functions=["SpringApplication", "run", "prepareEnvironment", "load",
               "handleRunFailure", "before", "name"],
    classes=["SpringApplication", "ApplicationRunner", "AbstractRunner"],
    imports=["java.util.Collections", "java.util.Set",
             "org.springframework.util.Assert.notNull"],
    # `refresh` in the Javadoc, `load` as a *call* inside load(), and the
    # `catch (Exception ex)` clause all have declaration shape.
    absent=["refresh", "createBeanDefinitionLoader", "handleExitCode", "catch",
            "warn", "notNull"],
)

KOTLIN = Case(
    "kotlin", "OkHttpClient.kt", mirrors="square/okhttp",
    source='''\
package okhttp3

import java.net.Proxy
import java.time.Duration
import okhttp3.internal.checkDuration

/** The HTTP client, in the shape okhttp3/OkHttpClient.kt uses. */
open class OkHttpClient internal constructor(
  builder: Builder,
) : Cloneable, Call.Factory {

  @get:JvmName("dispatcher")
  val dispatcher: Dispatcher = builder.dispatcher

  override fun newCall(request: Request): Call = RealCall(this, request, false)

  fun newWebSocket(
    request: Request,
    listener: WebSocketListener,
  ): WebSocket {
    val webSocket = RealWebSocket(taskRunner, request, listener)
    return webSocket
  }

  private suspend fun awaitClose(timeout: Duration): Boolean {
    // fun neverDeclared(): Unit {}
    val note = "fun alsoNeverDeclared() {}"
    return timeout.isZero
  }

  class Builder constructor() {
    internal var dispatcher: Dispatcher = Dispatcher()

    fun dispatcher(dispatcher: Dispatcher) = apply {
      this.dispatcher = dispatcher
    }
  }

  companion object {
    internal val DEFAULT_PROTOCOLS = listOf(Protocol.HTTP_2)
  }
}

interface Interceptor {
  fun intercept(chain: Chain): Response
}

fun Request.Builder.commonHeader(name: String, value: String) = apply {
  headers.set(name, value)
}
''',
    functions=["newCall", "newWebSocket", "awaitClose", "dispatcher",
               "intercept"],
    classes=["OkHttpClient", "Builder", "Interceptor"],
    imports=["java.net.Proxy", "java.time.Duration", "okhttp3.internal.checkDuration"],
    absent=["neverDeclared", "alsoNeverDeclared", "apply", "listOf"],
    # `fun Request.Builder.commonHeader` is missing from `functions` on
    # purpose — see TestKnownLimitations.test_kotlin_multi_segment_receiver.
)

GO = Case(
    "go", "hugobuilder.go", mirrors="gohugoio/hugo",
    source='''\
package commands

import (
	"context"
	"fmt"
	"sync"

	"github.com/gohugoio/hugo/common/loggers"
	"github.com/spf13/cobra"
)

// hugoBuilder mirrors commands/hugobuilder.go.
type hugoBuilder struct {
	r *rootCommand
	c *simpleCommand

	// func neverDeclared() {} — a declaration inside a comment
	mu sync.Mutex
}

type flagsToConfigHandler interface {
	flagsToConfig(cfg config.Provider)
}

func newHugoBuilder(r *rootCommand, s *serverCommand) *hugoBuilder {
	return &hugoBuilder{r: r, s: s}
}

func (c *hugoBuilder) build() error {
	stopCounter := make(chan struct{})
	if err := c.fullBuild(context.Background()); err != nil {
		return err
	}
	defer close(stopCounter)
	return nil
}

func (c *hugoBuilder) handleEvents(
	watcher *watcher.Batcher,
	staticSyncer *staticSyncer,
	evs []fsnotify.Event,
) {
	note := "func alsoNeverDeclared() {}"
	for _, ev := range evs {
		_ = ev
	}
	_ = note
}

func withTimeout(ctx context.Context, d time.Duration) (context.Context, func()) {
	return context.WithTimeout(ctx, d)
}
''',
    functions=["newHugoBuilder", "build", "handleEvents", "withTimeout"],
    classes=["hugoBuilder", "flagsToConfigHandler"],
    imports=[],
    absent=["neverDeclared", "alsoNeverDeclared", "flagsToConfig"],
    # Two deliberate omissions, both in TestKnownLimitations: `flagsToConfig`
    # is an interface member (no `func` keyword, so not a symbol), and the
    # grouped `import ( ... )` block yields no imports at all.
)

RUST = Case(
    "rust", "runtime.rs", mirrors="tokio-rs/tokio",
    source='''\
use crate::runtime::scheduler::CurrentThread;
use std::future::Future;
use std::time::Duration;

/// The Tokio runtime, in the shape tokio/src/runtime/runtime.rs uses.
#[derive(Debug)]
pub struct Runtime {
    scheduler: Scheduler,
    handle: Handle,
    blocking_pool: BlockingPool,
}

#[derive(Debug, Clone, Copy)]
pub enum RuntimeFlavor {
    CurrentThread,
    MultiThread,
}

pub trait Spawner {
    fn spawn_blocking<F, R>(&self, func: F) -> JoinHandle<R>;
}

impl Runtime {
    pub(crate) fn from_parts(
        scheduler: Scheduler,
        handle: Handle,
        blocking_pool: BlockingPool,
    ) -> Runtime {
        Runtime { scheduler, handle, blocking_pool }
    }

    pub fn block_on<F: Future>(&self, future: F) -> F::Output {
        // pub fn never_declared() {}
        let note = "pub fn also_never_declared() {}";
        let _ = note;
        self.handle.block_on(future)
    }

    pub async fn shutdown_timeout(self, duration: Duration) {
        if duration.is_zero() {
            return;
        }
    }

    unsafe fn raw_handle(&self) -> *const Handle {
        &self.handle as *const Handle
    }
}

impl Drop for Runtime {
    fn drop(&mut self) {
        match &mut self.scheduler {
            Scheduler::CurrentThread(_) => {}
            _ => {}
        }
    }
}

pub(crate) const fn max_blocking_threads() -> usize {
    512
}
''',
    functions=["from_parts", "block_on", "shutdown_timeout", "raw_handle",
               "drop", "max_blocking_threads", "spawn_blocking"],
    classes=["Runtime", "RuntimeFlavor", "Spawner"],
    imports=["crate::runtime::scheduler::CurrentThread", "std::future::Future",
             "std::time::Duration"],
    absent=["never_declared", "also_never_declared", "match", "if"],
)

C = Case(
    "c", "server.c", mirrors="redis/redis",
    source='''\
#include <stdio.h>
#include <stdlib.h>
#include "server.h"
#include "cluster.h"

/* Server state, in the shape src/server.c uses.
 * int never_declared(void) { — a declaration inside a block comment
 */
struct redisServer server;

typedef struct clientReplyBlock {
    size_t size, used;
    char buf[];
} clientReplyBlock;

enum ProtocolVersion {
    RESP2 = 2,
    RESP3 = 3
};

static void sigShutdownHandler(int sig) {
    char *msg = "static int also_never_declared(void) {";
    if (server.shutdown_asap && sig == SIGINT) {
        exit(1);
    }
    UNUSED(msg);
}

void initServerConfig(void) {
    int j;
    for (j = 0; j < CONFIG_DEFAULT_DBNUM; j++) {
        server.db[j].id = j;
    }
}

int processCommand(client *c) {
    if (!c->cmd) {
        rejectCommandFormat(c, "unknown command");
        return C_OK;
    }
    while (listLength(server.ready_keys)) {
        handleClientsBlockedOnKeys();
    }
    return C_OK;
}

int main(int argc, char **argv) {
    initServerConfig();
    return 0;
}
''',
    functions=["sigShutdownHandler", "initServerConfig", "processCommand", "main"],
    classes=["clientReplyBlock", "ProtocolVersion", "redisServer"],
    imports=["stdio.h", "stdlib.h", "server.h", "cluster.h"],
    absent=["never_declared", "also_never_declared", "if", "while",
            "rejectCommandFormat", "listLength"],
)

CPP = Case(
    "cpp", "txmempool.cpp", mirrors="bitcoin/bitcoin",
    source='''\
#include <txmempool.h>
#include <consensus/validation.h>
#include <util/moneystr.h>

// Mempool bookkeeping, in the shape src/txmempool.cpp uses.
class CTxMemPool
{
private:
    mutable RecursiveMutex cs;
    indexed_transaction_set mapTx;

public:
    explicit CTxMemPool(const Options& opts);

    void addUnchecked(const CTxMemPoolEntry& entry) EXCLUSIVE_LOCKS_REQUIRED(cs);
};

struct TxMempoolInfo
{
    CTransactionRef tx;
    int64_t nFeeDelta{0};
};

CTxMemPool::CTxMemPool(const Options& opts)
    : m_check_ratio{opts.check_ratio},
      m_max_size_bytes{opts.max_size_bytes}
{
    _clear();
}

void CTxMemPool::addUnchecked(const CTxMemPoolEntry &entry, setEntries &setAncestors)
{
    /* void never_declared() { */
    const std::string note{"void also_never_declared() {"};
    mapTx.insert(entry);
    if (setAncestors.size() > 0) {
        UpdateAncestorsOf(true, entry, setAncestors);
    }
}

bool CTxMemPool::CompareDepthAndScore(
    const uint256& hasha,
    const uint256& hashb,
    bool wtxid)
{
    LOCK(cs);
    return hasha < hashb;
}

static std::string HexStr(const Span<const uint8_t> s)
{
    std::string rv(s.size() * 2, '\\0');
    return rv;
}
''',
    functions=["HexStr"],
    classes=["CTxMemPool", "TxMempoolInfo"],
    imports=["txmempool.h", "consensus/validation.h", "util/moneystr.h"],
    absent=["never_declared", "also_never_declared", "if", "UpdateAncestorsOf"],
    # Only the free function is listed. Every `CTxMemPool::method` definition in
    # this file — which is to say the ordinary way C++ is written — is missed;
    # see TestKnownLimitations.test_cpp_out_of_line_definitions.
)

CSHARP = Case(
    "csharp", "ApplicationHost.cs", mirrors="jellyfin/jellyfin",
    source='''\
using System;
using System.Collections.Generic;
using System.Threading.Tasks;
using MediaBrowser.Controller;
using static System.Math;

namespace Emby.Server.Implementations
{
    /// <summary>
    /// Class ApplicationHost, in the shape ApplicationHost.cs uses.
    /// </summary>
    public class ApplicationHost : IServerApplicationHost, IAsyncDisposable
    {
        private readonly IFileSystem _fileSystem;

        public ApplicationHost(IServerApplicationPaths applicationPaths)
        {
            ApplicationPaths = applicationPaths;
        }

        public string Name => ApplicationProductName;

        public async Task InitAsync(IServiceCollection serviceCollection)
        {
            // public void NeverDeclared(int x) {}
            var note = "public void AlsoNeverDeclared() {}";
            DiscoverTypes();
            await Task.CompletedTask.ConfigureAwait(false);
        }

        protected virtual IEnumerable<Assembly> GetComposablePartAssemblies()
        {
            foreach (var p in _pluginManager.LoadedAssemblies)
            {
                yield return p;
            }
        }

        private void DiscoverTypes()
        {
            try
            {
                _allConcreteTypes = GetTypes();
            }
            catch (Exception ex)
            {
                Logger.LogError(ex, "Error loading types");
            }
        }
    }

    public interface IStartupEntryPoint
    {
        Task RunAsync();
    }

    public enum PackageStatus
    {
        Unknown = 0,
        Installed = 1
    }
}
''',
    functions=["ApplicationHost", "InitAsync", "GetComposablePartAssemblies",
               "DiscoverTypes"],
    classes=["ApplicationHost", "IStartupEntryPoint", "PackageStatus"],
    imports=["System", "System.Collections.Generic", "System.Threading.Tasks",
             "MediaBrowser.Controller", "System.Math"],
    absent=["NeverDeclared", "AlsoNeverDeclared", "catch", "foreach"],
    # C# opens method bodies on the *next* line. Those are found anyway,
    # because `logical_lines` folds a lone `{` up onto the signature. A member
    # with no body at all — `Task RunAsync();` in the interface — is not; see
    # TestKnownLimitations.test_csharp_interface_member.
)

RUBY = Case(
    "ruby", "base.rb", mirrors="rails/rails",
    source='''\
# frozen_string_literal: true

require "action_view"
require "action_controller/log_subscriber"
require_relative "metal/params_wrapper"

module ActionController
  # Base controller, in the shape actionpack/lib/action_controller/base.rb uses.
  class Base < Metal
    abstract!

    def self.without_modules(*modules)
      modules = modules.map do |m|
        m.is_a?(Symbol) ? ActionController.const_get(m) : m
      end
      MODULES - modules
    end

    def initialize(request = nil)
      # def never_declared; end
      @_response_body = nil
      super()
    end

    def process_action(method_name, *args)
      note = "def also_never_declared; end"
      run_callbacks(:process_action) do
        send_action(method_name, *args)
      end
    end

    def render_to_body(options = {})
      super || " "
    end

    def valid?
      !@_response_body.nil?
    end

    def reset!
      @_response_body = nil
    end

    private
      def _process_variant(options)
        options
      end
  end

  module Helpers
    def helper_method(*meths)
      meths.each { |meth| _helpers.define_method(meth) }
    end
  end
end
''',
    functions=["without_modules", "initialize", "process_action",
               "render_to_body", "valid?", "reset!", "_process_variant",
               "helper_method"],
    classes=["ActionController", "Base", "Helpers"],
    imports=["action_view", "action_controller/log_subscriber",
             "metal/params_wrapper"],
    absent=["never_declared", "also_never_declared"],
)

PHP = Case(
    "php", "Str.php", mirrors="laravel/framework",
    source='''\
<?php

namespace Illuminate\\Support;

use Illuminate\\Support\\Traits\\Macroable;
use Ramsey\\Uuid\\Uuid;
use voku\\helper\\ASCII;

require_once __DIR__.'/helpers.php';

/**
 * String helpers, in the shape src/Illuminate/Support/Str.php uses.
 */
class Str
{
    use Macroable;

    protected static $snakeCache = [];

    public static function of($string)
    {
        return new Stringable($string);
    }

    public static function after($subject, $search)
    {
        // public static function neverDeclared() {}
        $note = "public static function alsoNeverDeclared() {}";

        if ($search === '') {
            return $subject;
        }

        return array_reverse(explode($search, $subject, 2))[0];
    }

    public static function camel($value)
    {
        if (isset(static::$camelCache[$value])) {
            return static::$camelCache[$value];
        }

        return static::$camelCache[$value] = lcfirst(static::studly($value));
    }

    protected function replaceArray($search, $replace, $subject)
    {
        foreach ($replace as $value) {
            $subject = static::replaceFirst($search, $value, $subject);
        }

        return $subject;
    }
}

interface Htmlable
{
    public function toHtml();
}

trait Conditionable
{
    public function when($value, callable $callback)
    {
        return $this;
    }
}
''',
    functions=["of", "after", "camel", "replaceArray", "toHtml", "when"],
    classes=["Str", "Htmlable", "Conditionable"],
    imports=["Illuminate\\Support\\Traits\\Macroable", "Ramsey\\Uuid\\Uuid",
             "voku\\helper\\ASCII"],
    absent=["neverDeclared", "alsoNeverDeclared", "foreach"],
)

SWIFT = Case(
    "swift", "Session.swift", mirrors="Alamofire/Alamofire",
    source='''\
import Dispatch
import Foundation

/// Session, in the shape Source/Core/Session.swift uses.
open class Session {
    public static let `default` = Session()

    public let session: URLSession
    public let rootQueue: DispatchQueue

    public init(session: URLSession,
                delegate: SessionDelegate,
                rootQueue: DispatchQueue) {
        precondition(session.delegateQueue.underlyingQueue === rootQueue)
        self.session = session
        self.rootQueue = rootQueue
    }

    open func request(_ convertible: URLConvertible,
                      method: HTTPMethod = .get) -> DataRequest {
        // func neverDeclared() {}
        let note = "func alsoNeverDeclared() {}"
        _ = note
        return request(convertible, method: method)
    }

    func perform(_ request: Request) {
        guard !request.isCancelled else { return }
        rootQueue.async { self.activeRequests.insert(request) }
    }

    private func didCreateURLRequest(_ urlRequest: URLRequest) {
        if let handler = redirectHandler {
            _ = handler
        }
    }
}

public protocol RequestDelegate: AnyObject {
    func cleanup(after request: Request)
}

public struct HTTPHeaders {
    private var headers: [HTTPHeader] = []
}

extension Session: RequestDelegate {
    public func cleanup(after request: Request) {
        activeRequests.remove(request)
    }
}

enum AFError: Error {
    case invalidURL(url: URLConvertible)
}
''',
    functions=["request", "perform", "didCreateURLRequest", "cleanup"],
    classes=["Session", "RequestDelegate", "HTTPHeaders", "AFError"],
    imports=["Dispatch", "Foundation"],
    absent=["neverDeclared", "alsoNeverDeclared", "guard"],
    # `public init(...)` is a Swift initialiser, not a `func`, and the Swift
    # pattern is anchored on the `func` keyword — see TestKnownLimitations.
)

SCALA = Case(
    "scala", "Option.scala", mirrors="scala/scala",
    source='''\
package scala

import scala.language.implicitConversions
import scala.annotation.tailrec

/** Optional values, in the shape src/library/scala/Option.scala uses. */
object Option {
  implicit def option2Iterable[A](xo: Option[A]): Iterable[A] = xo.toList

  def apply[A](x: A): Option[A] = if (x == null) None else Some(x)

  def empty[A]: Option[A] = None
}

sealed abstract class Option[+A] extends IterableOnce[A] with Product {

  def isEmpty: Boolean

  final def isDefined: Boolean = !isEmpty

  final def getOrElse[B >: A](default: => B): B =
    if (isEmpty) default else this.get

  @inline final def map[B](f: A => B): Option[B] =
    if (isEmpty) None else Some(f(this.get))

  final def fold[B](ifEmpty: => B)(f: A => B): B = {
    // def neverDeclared: Int = 0
    val note = "def alsoNeverDeclared: Int = 0"
    if (isEmpty) ifEmpty else f(this.get)
  }

  @tailrec
  private def collectFirst[B](
    pf: PartialFunction[A, B]
  ): Option[B] = pf.lift(this.get)
}

final case class Some[+A](value: A) extends Option[A] {
  def isEmpty = false
}

trait IterableOnce[+A] {
  def iterator: Iterator[A]
}
''',
    # `implicit def option2Iterable` and `@inline final def map` are absent on
    # purpose: Scala's modifier list is closed, and neither `implicit` nor a
    # leading annotation is in it. See TestKnownLimitations.
    functions=["apply", "empty", "isEmpty", "isDefined",
               "getOrElse", "fold", "collectFirst", "iterator"],
    classes=["Option", "Some", "IterableOnce"],
    imports=["scala.language.implicitConversions", "scala.annotation.tailrec"],
    absent=["neverDeclared", "alsoNeverDeclared"],
)

DART = Case(
    "dart", "camera_controller.dart", mirrors="flutter/packages",
    source='''\
import 'dart:async';
import 'package:flutter/foundation.dart';
import 'package:camera_platform_interface/camera_platform_interface.dart';

/// Controls a device camera, in the shape packages/camera uses.
class CameraController extends ValueNotifier<CameraValue> {
  CameraController(
    this.description,
    this.resolutionPreset, {
    this.enableAudio = true,
  }) : super(const CameraValue.uninitialized());

  final CameraDescription description;

  int get cameraId => _cameraId;

  Future<void> initialize() async {
    // Future<void> neverDeclared() async {}
    final String note = "Future<void> alsoNeverDeclared() async {}";
    if (_isDisposed) {
      throw CameraException('Disposed', note);
    }
    _cameraId = await CameraPlatform.instance.createCamera(description);
  }

  Future<XFile> takePicture() async {
    _throwIfNotInitialized('takePicture');
    final XFile file = await CameraPlatform.instance.takePicture(_cameraId);
    return file;
  }

  void _throwIfNotInitialized(
    String functionName,
  ) {
    if (!value.isInitialized) {
      throw CameraException('Uninitialized', functionName);
    }
  }
}

mixin CameraLifecycle {
  void didChangeAppLifecycleState(AppLifecycleState state) {}
}

enum ResolutionPreset { low, medium, high }

abstract class CameraPlatform {
  Future<void> dispose(int cameraId);
}
''',
    functions=["initialize", "takePicture", "_throwIfNotInitialized",
               "didChangeAppLifecycleState"],
    classes=["CameraController", "CameraLifecycle", "ResolutionPreset",
             "CameraPlatform"],
    imports=["dart:async", "package:flutter/foundation.dart",
             "package:camera_platform_interface/camera_platform_interface.dart"],
    absent=["neverDeclared", "alsoNeverDeclared", "if"],
    # `Future<void> dispose(int cameraId);` — an abstract member with no body —
    # is missing on purpose; see TestKnownLimitations.
)

SHELL = Case(
    "shell", "cli.zsh", mirrors="ohmyzsh/ohmyzsh",
    source='''\
#!/usr/bin/env zsh

source "$ZSH/lib/functions.zsh"
. "$ZSH/lib/git.zsh"

# Command dispatch, in the shape lib/cli.zsh uses.
function _omz() {
  local -a cmds
  cmds=(help changelog plugin theme update)

  if (( $# == 0 )); then
    _omz::help
    return 1
  fi
}

_omz::help() {
  # never_declared() { }
  local note="also_never_declared() { }"
  cat >&2 <<EOF
Usage: omz <command> [options]
EOF
}

function _omz::plugin::list {
  local -a valid_plugins
  for plugin in "${valid_plugins[@]}"; do
    echo "$plugin"
  done
}

_omz::update() {
  local last_commit=$(builtin cd -q "$ZSH"; git rev-parse HEAD)
  while read -r line; do
    echo "$line"
  done
}
''',
    functions=["_omz"],
    classes=[],
    imports=[],
    absent=["never_declared", "also_never_declared", "if", "while", "for"],
    # Three of the four functions here are missing on purpose, and so are both
    # `source` lines. Namespaced zsh names (`_omz::help`), the brace-only form
    # (`function name {`) and the quoting of sourced paths are all in
    # TestKnownLimitations — this is the worst-served language in the set, and
    # ohmyzsh is written almost entirely in the forms it misses.
)

LUA = Case(
    "lua", "init.lua", mirrors="Kong/kong",
    source='''\
-- Kong entry point, in the shape kong/init.lua uses.
local kong_global = require "kong.global"
local constants = require("kong.constants")
local utils = require "kong.tools.utils"

local Kong = {}

local function parse_status_listeners(conf)
  local status_listeners = {}
  for _, listener in ipairs(conf.status_listeners) do
    status_listeners[#status_listeners + 1] = listener
  end
  return status_listeners
end

function Kong.init()
  -- local function never_declared() end
  local note = "function also_never_declared() end"
  local conf = kong_global.new()
  if conf.database == "off" then
    return conf
  end
  return conf
end

function Kong.init_worker()
  local worker_events, err = kong_global.init_worker_events()
  if not worker_events then
    return nil, err
  end
end

function Kong.access(ctx)
  ctx.KONG_ACCESS_START = get_updated_now_ms()
end

local function flush_delayed_response(ctx)
  while ctx.delayed_response do
    ngx.flush(true)
  end
end
''',
    functions=["parse_status_listeners", "Kong.init", "Kong.init_worker",
               "Kong.access", "flush_delayed_response"],
    classes=[],
    imports=["kong.global", "kong.constants", "kong.tools.utils"],
    absent=["never_declared", "also_never_declared", "if", "while"],
)

R = Case(
    "r", "geom-point.R", mirrors="tidyverse/ggplot2",
    source='''\
#' Points, in the shape R/geom-point.R uses.
library(grid)
library(rlang)
require(scales)

geom_point <- function(mapping = NULL, data = NULL,
                       stat = "identity", position = "identity",
                       ...,
                       na.rm = FALSE,
                       show.legend = NA) {
  # never_declared <- function() NULL
  note <- "also_never_declared <- function() NULL"
  layer(
    data = data,
    mapping = mapping,
    stat = stat,
    geom = GeomPoint
  )
}

draw_key_point <- function(data, params, size) {
  if (is.null(data$shape)) {
    data$shape <- 19
  }
  pointsGrob(0.5, 0.5, pch = data$shape)
}

translate_shape_string <- function(shape_string) {
  if (nchar(shape_string[1]) <= 1) {
    return(shape_string)
  }
  pch_table <- c("square open" = 0, "circle open" = 1)
  pch_table[shape_string]
}
''',
    functions=["geom_point", "draw_key_point", "translate_shape_string"],
    classes=[],
    imports=["grid", "rlang", "scales"],
    absent=["never_declared", "also_never_declared", "if"],
)

ELIXIR = Case(
    "elixir", "enum.ex", mirrors="elixir-lang/elixir",
    source='''\
defmodule Enum do
  @moduledoc """
  Functions over enumerables, in the shape lib/elixir/lib/enum.ex uses.

  def never_declared(x), do: x
  """

  import Kernel, except: [max: 2, min: 2]
  alias Enum.EmptyError
  require Logger
  use Bitwise

  @compile :inline_list_funcs

  def all?(enumerable) when is_list(enumerable) do
    predicate_list(enumerable, & &1)
  end

  def any?(enumerable, fun) do
    note = "def also_never_declared(x), do: x"
    _ = note
    Enumerable.reduce(enumerable, {:cont, false}, fun)
  end

  def at(enumerable, index, default \\\\ nil) do
    case slice_forward(enumerable, index, 1, 1) do
      [value] -> value
      [] -> default
    end
  end

  defp predicate_list([], _), do: true

  defp slice_forward(enumerable, start, amount, step) do
    if start < 0 do
      {:error, __MODULE__}
    else
      Enumerable.slice(enumerable)
    end
  end
end

defmodule Enum.EmptyError do
  defexception message: "empty error"
end
''',
    functions=["all?", "any?", "at", "predicate_list", "slice_forward"],
    classes=["Enum", "Enum.EmptyError"],
    imports=["Kernel", "Enum.EmptyError", "Logger", "Bitwise"],
    # `never_declared` lives inside the @moduledoc heredoc and *is* fabricated
    # into a symbol — the one language in this suite where the comment decoy
    # gets through. See TestKnownLimitations.test_elixir_heredoc.
    absent=["also_never_declared", "case", "if"],
)

CASES = [PYTHON, JAVASCRIPT, TYPESCRIPT, JAVA, KOTLIN, GO, RUST, C, CPP,
         CSHARP, RUBY, PHP, SWIFT, SCALA, DART, SHELL, LUA, R, ELIXIR]


def parse(case):
    fi = parse_source(case.filename, case.source)
    assert fi is not None, f"{case.filename} produced no FileInfo"
    return fi


def names(fi, kind=None):
    return [s.name for s in fi.symbols if kind is None or s.kind == kind]


class TestLanguageCoverage(unittest.TestCase):
    """Nineteen languages, one parse each, five properties per parse."""

    def test_every_supported_language_has_a_case(self):
        """A twentieth language must arrive with a test, not without one."""
        covered = {c.language for c in CASES}
        self.assertEqual(covered, set(LANGUAGES),
                         "languages without a parser test: "
                         f"{sorted(set(LANGUAGES) - covered)}")
        self.assertEqual(len(CASES), 19)

    def test_extension_maps_to_the_expected_language(self):
        for case in CASES:
            with self.subTest(language=case.language):
                name, defn = lang_for(case.filename)
                self.assertEqual(name, case.language)
                self.assertIsNotNone(defn)

    def test_functions_are_found(self):
        for case in CASES:
            with self.subTest(language=case.language, repo=case.mirrors):
                fi = parse(case)
                found = names(fi, "function")
                for want in case.functions:
                    self.assertIn(want, found,
                                  f"{case.language}: missing function {want!r}")

    def test_classes_and_types_are_found(self):
        for case in CASES:
            with self.subTest(language=case.language, repo=case.mirrors):
                fi = parse(case)
                found = names(fi, "class")
                for want in case.classes:
                    self.assertIn(want, found,
                                  f"{case.language}: missing type {want!r}")

    def test_imports_are_found(self):
        for case in CASES:
            with self.subTest(language=case.language, repo=case.mirrors):
                fi = parse(case)
                for want in case.imports:
                    self.assertIn(want, fi.imports,
                                  f"{case.language}: missing import {want!r}")

    def test_nothing_is_fabricated(self):
        """Comments, string literals and control flow produce no symbols.

        Every case carries a declaration inside a comment and another inside a
        string. Both are the exact shape that has fabricated symbols in this
        parser before, and a fabricated symbol is worse than a missing one: it
        takes the real definition's name out of the unique-name tier, so a
        correct call edge is dropped somewhere else in the repo.
        """
        for case in CASES:
            with self.subTest(language=case.language, repo=case.mirrors):
                fi = parse(case)
                found = set(names(fi))
                for banned in case.absent:
                    self.assertNotIn(banned, found,
                                     f"{case.language}: fabricated {banned!r}")

    def test_symbol_lines_point_at_the_declaration(self):
        """A symbol's line must be the line its name is actually on.

        Line numbers are attributes of a stable id, and every `outline`,
        `span` and editor jump trusts them. An off-by-one here is invisible in
        aggregate counts and wrong in every consumer.
        """
        for case in CASES:
            with self.subTest(language=case.language):
                lines = case.source.splitlines()
                fi = parse(case)
                for sym in fi.symbols:
                    self.assertGreaterEqual(sym.line, 1)
                    self.assertLessEqual(sym.line, len(lines))
                    text = lines[sym.line - 1]
                    leaf = sym.name.split(".")[-1].split("::")[-1]
                    self.assertIn(leaf, text,
                                  f"{case.language}: {sym.name} claims line "
                                  f"{sym.line}, which reads {text!r}")

    def test_symbol_spans_are_ordered_and_bounded(self):
        for case in CASES:
            with self.subTest(language=case.language):
                fi = parse(case)
                total = len(case.source.splitlines())
                previous = 0
                for sym in fi.symbols:
                    self.assertGreaterEqual(sym.line, previous,
                                            "symbols must come out in file order")
                    previous = sym.line
                    self.assertGreaterEqual(sym.length, 1)
                    self.assertLessEqual(sym.line + sym.length - 1, total + 1)
                    self.assertGreaterEqual(sym.complexity, 1)

    def test_line_accounting_adds_up(self):
        """code + comment + blank == loc, in every language."""
        for case in CASES:
            with self.subTest(language=case.language):
                fi = parse(case)
                self.assertEqual(
                    fi.code_lines + fi.comment_lines + fi.blank_lines, fi.loc)
                self.assertGreater(fi.code_lines, 0)
                self.assertGreater(fi.comment_lines, 0,
                                   "every case has at least one comment line")

    def test_parse_health_is_not_wildly_pessimistic(self):
        """`unparsed` is allowed to over-report, but not to cry wolf.

        The check is deliberately biased toward over-reporting — a false alarm
        costs a grep, a false all-clear costs a wrong decision. It still has to
        stay useful: if a clean file in a supported language reports more
        missed declarations than it has symbols, agents learn to ignore it.
        """
        for case in CASES:
            with self.subTest(language=case.language):
                fi = parse(case)
                self.assertLessEqual(
                    fi.unparsed, max(2, len(fi.symbols)),
                    f"{case.language}: {fi.unparsed} declaration-shaped lines "
                    f"unparsed against {len(fi.symbols)} symbols")


class TestPerLanguageDetail(unittest.TestCase):
    """The shapes worth naming individually, one language at a time."""

    def parse_snippet(self, filename, source):
        return parse_source(filename, source)

    def test_python_decorated_and_async_defs(self):
        fi = parse(PYTHON)
        by_name = {s.name: s for s in fi.symbols}
        self.assertEqual(by_name["QuerySet"].kind, "class")
        self.assertEqual(by_name["aget"].kind, "function")
        self.assertTrue(fi.has_type_hints is False or fi.has_type_hints is True)
        self.assertTrue(by_name["QuerySet"].documented,
                        "a class whose next line is a docstring is documented")

    def test_javascript_arrow_and_method_forms(self):
        fi = parse(JAVASCRIPT)
        found = names(fi, "function")
        self.assertIn("useCallback", found)       # const x = (a) => {}
        self.assertIn("useDebugValue", found)     # const x = async (a) => v
        self.assertIn("render", found)            # class method shorthand

    def test_typescript_interface_members_and_generics(self):
        fi = parse(TYPESCRIPT)
        found = names(fi)
        self.assertIn("init", found, "interface members end in `:`, which the "
                                     "TS method pattern accepts")
        self.assertIn("ApplicationScope", names(fi, "class"))
        self.assertIn("HttpsOptions", names(fi, "class"),
                      "`type X = {...}` is indexed as a type")

    def test_java_abstract_interface_and_wrapped_signatures(self):
        fi = parse(JAVA)
        found = names(fi, "function")
        self.assertIn("name", found, "abstract method, terminated by `;`")
        self.assertIn("run", found, "interface method, terminated by `;`")
        self.assertIn("prepareEnvironment", found, "signature wraps 3 lines")
        self.assertIn("load", found, "`throws` clause wraps to the next line")
        self.assertIn("SpringApplication", found, "constructor: no return type")

    def test_java_javadoc_reference_is_not_a_symbol(self):
        """`@see Foo#refresh()` is prose. It read as a same-file definition."""
        fi = parse(JAVA)
        self.assertNotIn("refresh", names(fi))

    def test_kotlin_single_segment_extension_keeps_the_bare_name(self):
        """`fun String.slug()` indexes as `slug`, not `String.slug`."""
        fi = parse_source("ext.kt", "fun String.slug(): String = this\n")
        self.assertEqual([s.name for s in fi.symbols], ["slug"])

    def test_go_receiver_methods_and_struct_types(self):
        fi = parse(GO)
        self.assertIn("build", names(fi, "function"))
        self.assertIn("handleEvents", names(fi, "function"))
        self.assertEqual(
            [s.kind for s in fi.symbols if s.name == "hugoBuilder"], ["class"])

    def test_go_single_line_import_is_found(self):
        """The one-line form works; the grouped block does not (see below)."""
        fi = parse_source("solo.go", 'package main\n\nimport "fmt"\n')
        self.assertEqual(fi.imports, ["fmt"])

    def test_rust_impl_block_is_not_a_type(self):
        """`impl Runtime` must not create a second `Runtime`.

        Two nodes with one name cost that name its unique-name tier in call
        resolution, which silently drops real edges elsewhere.
        """
        fi = parse(RUST)
        self.assertEqual(names(fi, "class").count("Runtime"), 1)
        self.assertIn("Spawner", names(fi, "class"))

    def test_c_static_and_pointer_returning_functions(self):
        fi = parse(C)
        found = names(fi, "function")
        self.assertIn("sigShutdownHandler", found)
        self.assertIn("main", found)
        self.assertNotIn("exit", found, "a call is not a declaration")

    def test_cpp_free_function_and_types(self):
        fi = parse(CPP)
        self.assertIn("HexStr", names(fi, "function"))
        self.assertIn("CTxMemPool", names(fi, "class"))
        self.assertIn("TxMempoolInfo", names(fi, "class"), "a struct is a type")

    def test_csharp_expression_bodied_and_async_members(self):
        fi = parse(CSHARP)
        found = names(fi, "function")
        self.assertIn("InitAsync", found)
        self.assertIn("ApplicationHost", found, "constructor")
        self.assertIn("PackageStatus", names(fi, "class"))

    def test_ruby_bang_and_question_method_names(self):
        fi = parse(RUBY)
        found = names(fi, "function")
        self.assertIn("valid?", found)
        self.assertIn("reset!", found)
        self.assertIn("without_modules", found, "`def self.x` keeps the name x")

    def test_php_use_trait_and_namespaced_import(self):
        fi = parse(PHP)
        self.assertIn("Conditionable", names(fi, "class"))
        self.assertIn("Illuminate\\Support\\Traits\\Macroable", fi.imports)

    def test_swift_extension_is_a_type_and_its_methods_are_functions(self):
        fi = parse(SWIFT)
        self.assertIn("Session", names(fi, "class"))
        self.assertIn("cleanup", names(fi, "function"))

    def test_scala_object_and_case_class(self):
        fi = parse(SCALA)
        types = names(fi, "class")
        self.assertIn("Option", types)
        self.assertIn("Some", types)
        self.assertIn("getOrElse", names(fi, "function"))

    def test_dart_named_parameters_and_mixins(self):
        fi = parse(DART)
        self.assertIn("CameraLifecycle", names(fi, "class"))
        self.assertIn("takePicture", names(fi, "function"))

    def test_shell_plain_function_forms(self):
        """Both undecorated forms work; only the namespaced ones do not."""
        fi = parse_source("f.sh", "function a() {\n  :\n}\n\nb() {\n  :\n}\n")
        self.assertEqual(names(fi, "function"), ["a", "b"])

    def test_lua_dotted_module_functions(self):
        fi = parse(LUA)
        found = names(fi, "function")
        self.assertIn("Kong.init", found)
        self.assertIn("parse_status_listeners", found, "local function")

    def test_r_assignment_defined_functions(self):
        fi = parse(R)
        self.assertIn("geom_point", names(fi, "function"))
        self.assertIn("scales", fi.imports, "require() is an import")

    def test_elixir_def_defp_and_module_names(self):
        fi = parse(ELIXIR)
        self.assertIn("predicate_list", names(fi, "function"), "defp")
        self.assertIn("all?", names(fi, "function"))
        self.assertIn("Enum.EmptyError", names(fi, "class"),
                      "defmodule keeps its dotted name")

    def test_php_trait_use_reads_as_a_dependency(self):
        """`use Macroable;` inside a class body lands in imports.

        Documented rather than judged: in Laravel that trait really is a
        dependency of the class, and the file also imports it by namespace one
        line earlier. Worth knowing it is there, because it resolves by
        basename like any other import.
        """
        fi = parse(PHP)
        self.assertIn("Macroable", fi.imports)


class TestKnownLimitations(unittest.TestCase):
    """The defect register: what a correct parser would do, and does not.

    Every test here asserts *correct* behaviour and is marked expected-failure,
    so the suite stays green while the list stays honest. Fix one of these in
    `engine/languages.py` and unittest reports an unexpected success, which
    fails the run until the entry below is deleted.

    None of these is a mystery: they are the price of one regex per language
    instead of a grammar. They are written down so the price is visible, and so
    `overview`'s coverage numbers on these languages can be read for what they
    are.
    """

    @unittest.expectedFailure
    def test_go_grouped_import_block(self):
        """`import ( "fmt" \\n "sync" )` yields no imports at all.

        Each line inside the block is nothing but a string literal. Imports are
        scanned from a string-*preserving* view for exactly this reason — but
        the loop classifies lines using the string-*blanked* view first, and a
        line that is empty there is counted as a comment and skipped before the
        import scan ever runs. So Go's normal import syntax is invisible, and
        only the rare one-line `import "fmt"` form is seen.

        This is the largest single finding in this suite: it costs Go every
        file-to-file import edge in a repository written the ordinary way.
        """
        fi = parse(GO)
        self.assertIn("github.com/spf13/cobra", fi.imports)
        self.assertIn("sync", fi.imports)

    @unittest.expectedFailure
    def test_go_interface_members(self):
        """Members of a `type X interface { ... }` block are not symbols.

        The Go function pattern is anchored on the `func` keyword, which an
        interface member does not have. `who-calls` on such a method therefore
        answers about the implementations only.
        """
        self.assertIn("flagsToConfig", names(parse(GO), "function"))

    @unittest.expectedFailure
    def test_cpp_out_of_line_definitions(self):
        """`void CTxMemPool::addUnchecked(...)` is not indexed.

        The C++ pattern wants `<type> <name>(` with only word characters
        between them, and `CTxMemPool::addUnchecked` puts a `::` where the
        space should be. Out-of-line definition is how C++ is normally written,
        so what survives in a .cpp file is mostly free functions.
        """
        found = names(parse(CPP), "function")
        self.assertIn("addUnchecked", found)
        self.assertIn("CompareDepthAndScore", found)

    @unittest.expectedFailure
    def test_kotlin_multi_segment_receiver(self):
        """`fun Request.Builder.commonHeader(...)` indexes as `Builder`.

        The extension-receiver group allows exactly one dotted segment, so on a
        two-segment receiver the regex consumes `Request.` and captures
        `Builder` as the function name. This is a fabrication, not an omission:
        a function node named `Builder` now shares a name with the real
        `Builder` class, which costs that name its unique-name tier and drops
        real call edges elsewhere.
        """
        found = names(parse(KOTLIN), "function")
        self.assertIn("commonHeader", found)
        self.assertNotIn("Builder", found)

    @unittest.expectedFailure
    def test_elixir_heredoc(self):
        """`@moduledoc \"\"\"...\"\"\"` prose is parsed as code.

        `_TRIPLE` in the parser lists python, java, kotlin, scala and groovy —
        not elixir — so an Elixir heredoc is never blanked, and the `def` in a
        doc example becomes a symbol. Every idiomatic Elixir module carries a
        @moduledoc with examples in it.
        """
        self.assertNotIn("never_declared", names(parse(ELIXIR)))

    @unittest.expectedFailure
    def test_shell_namespaced_function_names(self):
        """`_omz::help() {` is not a function.

        The shell pattern's name group is `[A-Za-z_]\\w*`, which stops at the
        first `:`. zsh's `::` namespacing is ubiquitous — in ohmyzsh it is the
        house style — so this is most of that repository's functions.
        """
        found = names(parse(SHELL), "function")
        self.assertIn("_omz::help", found)
        self.assertIn("_omz::update", found)

    @unittest.expectedFailure
    def test_shell_brace_only_function_form(self):
        """`function name {` — legal in bash and zsh — needs no parentheses."""
        fi = parse_source("f.zsh", "function only_braces {\n  :\n}\n")
        self.assertIn("only_braces", names(fi, "function"))

    @unittest.expectedFailure
    def test_shell_sourced_path_keeps_its_quotes(self):
        """`source "$ZSH/lib/git.zsh"` imports `"$ZSH/lib/git.zsh"`, quotes and all.

        The captured group is `[^\\s]+`, which happily includes the quote
        characters, so the import can never match a real path and the edge is
        dropped downstream. Harmless to correctness, fatal to usefulness.
        """
        self.assertIn("$ZSH/lib/git.zsh", parse(SHELL).imports)

    @unittest.expectedFailure
    def test_scala_implicit_and_annotated_defs(self):
        """`implicit def` and `@inline final def` are not matched.

        Scala's modifier list in the pattern is private/protected/override/
        final. `implicit`, `lazy`, `abstract` and a leading annotation all
        break the match, and they are common in the standard library this case
        mirrors.
        """
        found = names(parse(SCALA), "function")
        self.assertIn("option2Iterable", found)
        self.assertIn("map", found)

    @unittest.expectedFailure
    def test_swift_initialisers(self):
        """`public init(...)` is a declaration; the Swift pattern needs `func`."""
        self.assertIn("init", names(parse(SWIFT), "function"))

    @unittest.expectedFailure
    def test_swift_extension_duplicates_the_type(self):
        """`extension Session: RequestDelegate` creates a second `Session`.

        Rust's `impl` blocks are deliberately excluded from the type pattern
        for exactly this reason — two nodes with one name cost that name its
        unique-name tier in call resolution. Swift's `extension` is the same
        construct and is not excluded.
        """
        self.assertEqual(names(parse(SWIFT), "class").count("Session"), 1)

    @unittest.expectedFailure
    def test_csharp_interface_member(self):
        """`Task RunAsync();` in an interface has no body and is not found."""
        self.assertIn("RunAsync", names(parse(CSHARP), "function"))

    @unittest.expectedFailure
    def test_dart_abstract_member(self):
        """`Future<void> dispose(int cameraId);` — abstract, so no `{`."""
        self.assertIn("dispose", names(parse(DART), "function"))


class TestUnknownLanguages(unittest.TestCase):
    """The twentieth language is the one Peacock has never heard of."""

    def test_unknown_extension_is_not_code(self):
        self.assertEqual(lang_for("weird.zzz"), (None, None))
        self.assertIsNone(parse_source("weird.zzz", "fn main() {}\n"))

    def test_docs_and_config_still_count_toward_shape(self):
        doc = parse_source("README.md", "# Title\n\nSome prose.\n")
        self.assertEqual(doc.kind, "doc")
        self.assertEqual(doc.loc, 3)
        cfg = parse_source("pom.xml", "<project>\n  <name>x</name>\n</project>\n")
        self.assertEqual(cfg.kind, "config")
        self.assertEqual(cfg.symbols, [])

    def test_generated_and_minified_files_are_skipped(self):
        for path in ("bundle.min.js", "types.d.ts", "yarn.lock", "app.js.map"):
            with self.subTest(path=path):
                self.assertEqual(lang_for(path), (None, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
