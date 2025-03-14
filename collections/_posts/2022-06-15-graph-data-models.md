---
layout:     post  
title:      Unraveling Connections- Exploring Graph-Like Data Models
date:       2022-06-15
summary:    A deep dive into graph-based data models, their structures, and query languages like Cypher, SPARQL, and Datalog.
categories: data-models databases ddia
---

In scenarios where complex many-to-many relationships dominate, graph-like data models provide a natural and highly efficient solution. These models are well-suited to use cases such as social networks, recommendation engines, and road networks. This post explores the fundamentals of graph models, their key implementations, and examples of queries using popular graph query languages.

---  

### **Understanding the Basics of Graph Data Models**

A graph consists of:    
**Vertices (Nodes):** Represent entities (people, locations, web pages).    
**Edges (Relationships):** Represent how entities are connected.

#### Examples of Graph Use Cases:

1. **Social Networks:**    
   Vertices = People; Edges = Friendships.

2. **Web Graphs:**    
   Vertices = Web Pages; Edges = Hyperlinks.

3. **Road Networks:**    
   Vertices = Intersections; Edges = Roads or Rails.

---  

### **The Property Graph Model**

Widely implemented in graph databases like Neo4j, Titan, and InfiniteGraph, the property graph model assigns attributes to both vertices and edges.

#### Features of a Property Graph:

- **Vertices:**
    - Unique identifiers.
    - Set of outgoing and incoming edges.
    - A collection of properties stored as key-value pairs.

- **Edges:**
    - Unique identifiers.
    - Tail vertex, head vertex.
    - A label describing the relationship.
    - A collection of properties.

SQL Schema Representation:
```sql  
CREATE TABLE vertices (  
    vertex_id   integer PRIMARY KEY,  
    properties  json  
);  
   
CREATE TABLE edges (  
    edge_id     integer PRIMARY KEY,  
    tail_vertex integer REFERENCES vertices (vertex_id),  
    head_vertex integer REFERENCES vertices (vertex_id),  
    label       text,  
    properties  json  
);  
```  
   
---  

### **Querying Graphs: Cypher Language**

Cypher, the primary query language for Neo4j, provides a declarative syntax for traversing graphs.

#### Example: Insert Vertices and Edges
```cypher  
CREATE  
  (NAmerica:Location {name:'North America', type:'continent'}),  
  (USA:Location      {name:'United States', type:'country'}),  
  (Idaho:Location    {name:'Idaho', type:'state'}),  
  (Lucy:Person       {name:'Lucy'}),  
  (Idaho) -[:WITHIN]->  (USA)  -[:WITHIN]-> (NAmerica),  
  (Lucy)  -[:BORN_IN]-> (Idaho)  
```  

##### Representation:
```plaintext  
        North America  
             ↑  
       +------+  
       |WITHIN|  
       |      |  
      USA    Idaho  
       ↑       ↑  
    (Lucy)  BORN_IN  
```  

#### Example: Complex Query in Cypher
Find all people born in the US and living in Europe:

```cypher  
MATCH  
  (person) -[:BORN_IN]-> () -[:WITHIN*0..]-> (us:Location {name:'United States'}),  
  (person) -[:LIVES_IN]-> () -[:WITHIN*0..]-> (eu:Location {name:'Europe'})  
RETURN person.name  
```  

Explanation:
1. Locate people with a `BORN_IN` relationship to any location within the US.
2. Check if they also have a `LIVES_IN` relationship to a European location.
3. Return their names.

---  

### **Triple-Stores and SPARQL**

The **triple-store model**, common in RDF databases, follows the structure of subject-predicate-object to represent relationships:    
**Example Triple:** `(Jim, likes, bananas)`

In SPARQL, you can query triples declaratively:
```sparql  
SELECT ?person WHERE {  
  ?person :bornIn :usa .  
  ?person :livesIn :europe .  
}  
```  
   
---  

### **Datalog Approach - Example Query**

Datalog uses simple facts and logical rules:    
**Facts:**
```prolog  
name(usa, 'United States').    
type(usa, country).    
within(idaho, usa).    
```  

**Rules:**
```prolog  
within_recursive(Location, Name) :- name(Location, Name).  
within_recursive(Location, Name) :- within(Location, SubLocation), within_recursive(SubLocation, Name).  
```  

**Query:**    
To find people who migrated from the US to Europe:
```prolog  
emigrated(Person,