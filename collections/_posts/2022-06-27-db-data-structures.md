---
layout:     post    
title:      The Backbone of Databases- Data Structures that Power Storage
date:       2022-06-27    
summary:    Dive into the essential data structures, including hash indexes, B-trees, and LSM-trees, that enable efficient storage and retrieval in databases.    
categories: storage data-structures databases ddia
---

At the heart of every database lies a powerful combination of data structures designed to efficiently store and retrieve information. This post explores some of the essential mechanisms—including **hash indexes**, **B-trees**, and **LSM-trees**—that drive the performance of modern database systems.
   
---  

### **Building Blocks: A Simple Key-Value Store**

Before diving into advanced data structures, let’s start with a naive implementation of a database:

```bash  
#!/bin/bash  
   
db_set () {  
    echo "$1,$2" >> database  
}  
   
db_get () {  
    grep "^$1," database | sed -e "s/^$1,//" | tail -n 1  
}  
```  

This script does the following:
1. Stores key-value pairs by appending them to a file (`db_set`).
2. Retrieves the latest value for a key by scanning the file (`db_get`).

#### Pros and Drawbacks
- **Pros:** Simple, efficient `O(1)` for writes (append-only).
- **Cons:** Horrendous `O(n)` lookup performance as the file grows.

Introducing **indexes** solves the lookup inefficiency by adding metadata to help locate data quickly and eliminate unnecessary scans.
   
---  

### **Hash Indexes: Fast Lookup for Key-Value Stores**

A **hash index** uses a hash map to store the location (byte offset) of each key in the data file. When searching for a key, the hash directs you instantly to the relevant portion of the file.

#### How It Works:
```plaintext  
 +------------+----------+  
 | Key        | Offset   |  
 +------------+----------+  
 | london     | 100      |  
 | san_francisco | 200   |  
 +------------+----------+  
```  

For example:
```json  
{  
    "123456": {"city": "London", "attractions": ["Big Ben", "London Eye"]},  
    "42": {"city": "San Francisco", "attractions": ["Golden Gate Bridge"]}  
}  
```  

Whenever you write data, the offset is updated in the hash table. This approach works wonders for small-to-moderate datasets thanks to its memory dependency for high-speed lookups.
   
---  

### **SSTables and LSM-Trees: Sequential Efficiency**

When you need to sort and store large datasets while maintaining high write speeds, the **Sorted String Table** (SSTable) is a perfect choice. It relies on immutable, sorted write batches stored sequentially in files to optimize I/O performance.

#### Workflow:
1. **Write:** Data first lands in an in-memory balanced tree, called a **memtable**.
2. **Flush:** When the memtable fills up, it’s dumped to disk in a sorted SSTable.
3. **Merge:** From time to time, segments of files are merged and compacted in the background to manage disk space.

#### Benefits and Limitations:
- **Advantages:** Sequential disk writes = blazing fast! Perfect for key-value stores like Cassandra and HBase.
- **Challenges:** Slower lookups for nonexistent keys without extra data structures (e.g., Bloom filters).

Representation of an LSM-Tree:
```plaintext  
[memtable in-memory]  
       ↓ Flush  
[SSTable1] [SSTable2]...  
       ↓ Merge/Compact  
   [Updated SSTable]  
```  
   
---  

### **B-Trees: The Hidden Champions**

**B-trees** have been the backbone of relational databases for decades, powering the indexing in systems like MySQL and PostgreSQL. Unlike SSTables, B-trees rewrite pages dynamically instead of using append-only segments.

#### How B-Trees Work:
1. Divide data into **pages** (e.g., 4 KB per block).
2. Use a **tree** structure with pointers to efficiently locate pages.
3. Perform lookups, updates, and inserts by traversing the tree.

#### Example Process:
Imagine looking for key `251`:
```plaintext  
           [Root: 200, 300]  
               ↓  
      [200–300 Page: 200, 250]  
               ↓  
    Leaf Page: [250, 251, 252]  
 ```  

Key Features:
- **Logarithmic Depth:** O(log n) lookups ensure performance is robust for large datasets.
- **Dynamic Updates:** Supports both insertion and modification efficiently.

---  

### **Trade-offs and When to Use Each Structure**

| **Data Structure** | **Advantages**                             | **Disadvantages**                         | **Use Cases**                                                                          |  
|---------------------|-------------------------------------------|-------------------------------------------|---------------------------------------------------------------------------------------|  
| **Hash Indexes**    | Super-fast lookups by key. Minimal memory usage.   | Poor range query performance. Relies on in-memory index.      | Small key-value stores with high lookup speeds like Bitcask (Riak).                   |  
| **LSM-Trees**       | High write throughput. Optimized for sequential reads. | Merging overhead. Slower for random reads.                       | Distributed systems with high write load (Cassandra, HBase).                          |  
| **B-Trees**         | Balanced reads/writes. Supports efficient range queries. | Requires more bookkeeping & concurrency controls.             | Dominant in relational databases and applications with mixed workload needs.          |  
   
---  

### **Combining Approaches for Optimal Performance**

Modern databases often blend these structures to handle different workloads. For instance:

1. **MongoDB:** Uses a combination of hash indexes and B-tree variants for flexible query support.
2. **Cassandra:** Employs LSM-tree mechanisms for fast write-heavy workloads.

Each structure comes with its own set of trade-offs, which means there's no one-size-fits-all solution. By understanding these building blocks, developers can select or tune storage engines that best meet their needs.
   
---  

### **Conclusion: The Behind-the-Scenes Magic**

The unseen heroes—**data structures**—are critical to a database's ability to balance write speeds, lookup efficiency, and range queries. Whether it’s the simplicity of a hash index, the sequential efficiency of an LSM-tree, or the balanced versatility of a B-tree, each structure contributes to the incredible performance databases achieve today.

Understanding these mechanisms not only demystifies how databases work but also empowers you to make informed decisions when choosing or optimizing storage engines for your next project.