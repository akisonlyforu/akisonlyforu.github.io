---
layout:     post    
title:      Unbundling Monolithic Databases for Flexibility
date:       2023-02-26
summary:    Learn how unbundling databases helps to achieve scalability and flexibility, combining specialized tools to meet modern data needs.    
categories: ddia future-data-systems unbundling-system-design
---

### **Introduction**

Databases have traditionally consolidated functionalities, combining features like storage, indexing, query processing, and replication into one integrated system. However, as application requirements diversify, handling everything in one database becomes impractical. **Unbundling databases** proposes breaking monolithic systems into smaller, specialized components. These components work collectively but focus on mastering specific responsibilities, such as full-text search, analytics, or change data capture.

The unbundling approach not only increases flexibility but also aligns with the Unix philosophy of modularity—combining small tools with well-defined purposes to create reliable, scalable systems.
   
---  

### **Why Unbundle Databases?**

The one-size-fits-all database model often falls short when the application requires niche capabilities that a general-purpose DBMS cannot excel at (e.g., full-text searches, graph traversals, or machine learning). Breaking down databases enables:
1. **Specialized Performance**: Each component is optimized for specific workloads, improving performance across diverse access patterns.
2. **Resilience and Scalability**: Independent components reduce complexity and enable more fine-grained fault-tolerance strategies.

#### **The Role of Derived Data**
- Unbundling databases aligns closely with the **derived data architecture**.
- Secondary indexes, materialized views, caches, and full-text indexes may be externalized rather than built into a single database, using tools most suited to their tasks (e.g., Elasticsearch for full-text indexing).

This approach retains the flexibility to build pipelines that use multiple components, balancing performance and functionality across specialized systems.
   
---  

### **Composing Data Storage Technologies**

Unbundling databases means using different specialized systems and coordinating them for broader workflows. Components in an unbundled architecture include:
1. **Materialized Views**: Precomputed aggregations for fast query response.
2. **Replication Logs**: Coordinating data synchronization across multiple storage systems.
3. **Search Engines**: External systems (such as Elasticsearch) tailored for full-text or fuzzy queries.

Unbundling emphasizes breadth—providing broad coverage for multiple workloads—over deep optimization for individual use cases.

Example:     
Imagine using **distributed blob storage** to hold raw datasets, **a graph database** for social network queries, and **Kafka streams** for log-based synchronization, all integrating seamlessly for specific application needs.
   
---  

### **Challenges in Unbundled Systems**

While unbundling databases has advantages, challenges arise in orchestrating these components:
1. **Dataflow Complexity**: Unbundling demands careful attention to how data flows between systems, possibly requiring custom code for communication and synchronization.
2. **Write Synchronization**: Ensuring every system participating in unbundling receives writes reliably (e.g., through event logs) is crucial. A single disagreement may lead to data divergence.
3. **Operational Overhead**: Each piece of infrastructure introduces its own operational quirks (e.g., scaling, fault tolerance), which can increase administrative costs.

---  

### **Designing Applications Around Dataflow**

In an unbundled database world, applications are seen as **derivation functions** feeding off a stream of state changes. These flows involve:
1. **Event Logs for Dataflow Coordination**: Systems like Kafka or Pulsar provide a write-ahead log to synchronize streams with downstream systems like caches or materialized views.
2. **Continuous Derivations**: Secondary indexes, full-text search indexes, and cached views are derived in near real-time through automation, minimizing manual coordination.

Dataflow-friendly architectures emphasize loose coupling between tools while offering predictable results.
  
---  

### **Conclusion**

Unbundling databases enables organizations to leverage highly specialized tools for diverse workloads while maintaining scalable and resilient systems. Although unbundling introduces challenges in coordination and integration, its modular approach fosters long-term extensibility. By managing derived dataflows and embracing the diversity of modern database tools, engineers can create systems tailored to the evolving complexities of data processing. This trend is set to grow as applications increasingly prioritize flexibility and performance.  