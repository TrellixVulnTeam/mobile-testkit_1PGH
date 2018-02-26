﻿// 
//  Program.cs
// 
//  Author:
//   Jim Borden  <jim.borden@couchbase.com>
// 
//  Copyright (c) 2017 Couchbase, Inc All rights reserved.
// 
//  Licensed under the Apache License, Version 2.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at
// 
//  http://www.apache.org/licenses/LICENSE-2.0
// 
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
// 

using System;
using System.Collections.Generic;

using HandlerAction = System.Action<System.Collections.Specialized.NameValueCollection, 
    System.Collections.Generic.IReadOnlyDictionary<string, object>, 
    System.Net.HttpListenerResponse>;

namespace Couchbase.Lite.Testing.NetCore
{
    class Program
    {
        #region Private Methods

        private static void Extend()
        {
            Router.Extend(new Dictionary<string, HandlerAction>
            {
                ["start_sync_gateway"] = OrchestrationMethods.StartSyncGateway,
                ["kill_sync_gateway"] = OrchestrationMethods.KillSyncGateway,
                //["compile_query"] = QueryMethods.CompileQuery,
                ["start_cb_server"] = OrchestrationMethods.StartCouchbaseServer,
                ["stop_cb_server"] = OrchestrationMethods.StopCouchbaseServer
            });
        }

        static void Main(string[] args)
        {
            Couchbase.Lite.Support.NetDesktop.Activate();
            Extend();

            Couchbase.Lite.Support.NetDesktop.EnableTextLogging("TextLogging");
            Database.SetLogLevel(Logging.LogDomain.All, Logging.LogLevel.Info);

            var listener = new TestServer();
            listener.Start();

            Console.WriteLine("Press any key to exit...");
            Console.ReadKey(true);

            listener.Stop();
        }

        #endregion
    }
}
